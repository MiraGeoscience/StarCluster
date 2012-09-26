import os
import re
import time
import gzip
import email
import base64
import tarfile
import StringIO

from email import encoders
from email.mime import base
from email.mime import text
from email.mime import multipart

from starcluster import utils
from starcluster import exception

starts_with_mappings = {
    '#include': 'text/x-include-url',
    '#!': 'text/x-shellscript',
    '#cloud-config': 'text/cloud-config',
    '#cloud-config-archive': 'text/cloud-config-archive',
    '#upstart-job': 'text/upstart-job',
    '#part-handler': 'text/part-handler',
    '#cloud-boothook': 'text/cloud-boothook',
    '#ignored': 'text/ignore'
}


def _get_type_from_fp(fp):
    line = fp.readline()
    fp.seek(0)
    # slist is sorted longest first
    slist = starts_with_mappings.keys()
    slist.sort(key=lambda e: -1 * len(e))
    for sstr in slist:
        if line.startswith(sstr):
            return starts_with_mappings[sstr]
    raise exception.BaseException("invalid user data type: %s" % line)


def mp_userdata_from_files(files, compress=False):
    outer = multipart.MIMEMultipart()
    mtypes = []
    for i, fp in enumerate(files):
        mtype = _get_type_from_fp(fp)
        mtypes.append(mtype)
        maintype, subtype = mtype.split('/', 1)
        if maintype == 'text':
            # Note: we should handle calculating the charset
            msg = text.MIMEText(fp.read(), _subtype=subtype)
            fp.close()
        else:
            if hasattr(fp, 'name'):
                fp = open(fp.name, 'rb')
            msg = base.MIMEBase(maintype, subtype)
            msg.set_payload(fp.read())
            fp.close()
            # Encode the payload using Base64
            encoders.encode_base64(msg)
        # Set the filename parameter
        fname = getattr(fp, 'name', "sc_%d" % i)
        msg.add_header('Content-Disposition', 'attachment',
                       filename=os.path.basename(fname))
        outer.attach(msg)
    userdata = outer.as_string()
    if compress:
        s = StringIO.StringIO()
        gfile = gzip.GzipFile(fileobj=s, mode='w')
        gfile.write(userdata)
        gfile.close()
        s.seek(0)
        userdata = s.read()
    return userdata


def get_mp_from_userdata(userdata, decompress=False):
    if decompress:
        zfile = StringIO.StringIO(userdata)
        gfile = gzip.GzipFile(fileobj=zfile, mode='r')
        userdata = gfile.read()
        gfile.close()
    return email.message_from_string(userdata)


SCRIPT_TEMPLATE = """\
#!/usr/bin/env python
import os, sys, stat, gzip, base64, tarfile, StringIO
os.chdir(os.path.dirname(sys.argv[0]))
decoded = StringIO.StringIO(base64.b64decode('''%s'''))
gf = gzip.GzipFile(mode='r', fileobj=decoded)
tf = tarfile.TarFile(mode='r', fileobj=gf)
for ti in tf:
    tf.extract(ti)
    is_exec = (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) & ti.mode != 0
    if ti.isfile() and is_exec:
        os.system(os.path.abspath(ti.name))
"""


def userdata_script_from_files(fileobjs, tar_fname=None):
    tar_fname = tar_fname or 'sc_userdata.tar'
    tfd = StringIO.StringIO()
    tf = tarfile.TarFile(tar_fname, mode='w', fileobj=tfd)
    for f in fileobjs:
        if hasattr(f, 'fileno'):
            ti = tf.gettarinfo(fileobj=f)
        else:
            ti = tarfile.TarInfo()
        ti.name = os.path.basename(f.name)
        ti.mtime = time.time()
        if f.read(2) == '#!':
            ti.mode = 0755
        f.seek(0)
        if hasattr(f, 'buf'):
            ti.size = len(f.buf)
        tf.addfile(ti, f)
    tf.close()
    tfd.seek(0)
    gfd = StringIO.StringIO()
    gzip_fname = os.path.extsep.join([tar_fname, '.gz'])
    gf = gzip.GzipFile(gzip_fname, mode='w', fileobj=gfd)
    gf.write(tfd.read())
    gf.close()
    gfd.seek(0)
    gfs = StringIO.StringIO(gfd.read())
    b64str = base64.b64encode(gfs.read())
    script = SCRIPT_TEMPLATE % b64str
    return script


def get_tar_from_userdata(string):
    r = re.compile("b64decode\('''(.*)'''\)")
    b64str = r.search(string).groups()[0]
    gzf = StringIO.StringIO(b64str.decode('base64'))
    tarstr = StringIO.StringIO(gzip.GzipFile(fileobj=gzf, mode='r').read())
    return tarfile.TarFile(fileobj=tarstr, mode='r')

ENABLE_ROOT_LOGIN_SCRIPT = """\
#!/usr/bin/env python
import re;
r = re.compile(',?command=".*",?')
akf = '/root/.ssh/authorized_keys'
fixed = r.subn('', open(akf).read())[0]
open(akf, 'w').write(fixed)
"""


def bundle_userdata_files(fileobjs, tar_fname=None, compress=True,
                          use_cloudinit=True):
    script_type = starts_with_mappings['#!']
    ignored_type = starts_with_mappings['#ignored']
    for i, fobj in enumerate(fileobjs):
        ftype = _get_type_from_fp(fobj)
        if ftype == ignored_type:
            fileobjs[i] = utils.string_to_file("#!/bin/false\n" + fobj.read(),
                                               fobj.name)
            continue
        elif ftype != script_type:
            use_cloudinit = True
    if use_cloudinit:
        fileobjs += [utils.string_to_file('#cloud-config\ndisable_root: 0',
                                          'starcluster_cloud_config.txt')]
        return mp_userdata_from_files(fileobjs, compress=compress)
    else:
        fileobjs += [utils.string_to_file(ENABLE_ROOT_LOGIN_SCRIPT,
                                          'starcluster_enable_root_login.sh')]
        return userdata_script_from_files(fileobjs, tar_fname=tar_fname)


def unbundle_userdata(string, decompress=True):
    udata = {}
    if string.startswith('#!'):
        tf = get_tar_from_userdata(string)
        files = tf.getmembers()
        for f in files:
            udata[f.name] = tf.extractfile(f).read()
    else:
        mpmime = get_mp_from_userdata(string, decompress=decompress)
        files = mpmime.get_payload()
        for f in files:
            udata[f.get_filename()] = f.get_payload()
    return udata


if __name__ == '__main__':
    files = utils.strings_to_files(['#!/bin/bash\nhostname',
                                    '#!/bin/bash\ndate'],
                                   fname_prefix='sc_userdata_file')
    files += utils.string_to_file('#ignored\nblahblahblah', 'sc_metadata')
    script = bundle_userdata_files(files, use_cloudinit=False)
    f = open('/tmp/tester.sh', 'w')
    f.write(script)
    f.close()
    os.chmod('/tmp/tester.sh', 0750)
