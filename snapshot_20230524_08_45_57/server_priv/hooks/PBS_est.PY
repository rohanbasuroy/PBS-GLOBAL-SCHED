# config: utf-8

# Copyright (C) 2003-2022 Altair Engineering, Inc. All rights reserved.
# Copyright notice does not imply publication.
#
# ALTAIR ENGINEERING INC. Proprietary and Confidential. Contains Trade Secret
# Information. Not for use or disclosure outside of Licensee's organization.
# The software and information contained herein may only be used internally and
# is provided on a non-exclusive, non-transferable basis. License may not
# sublicense, sell, lend, assign, rent, distribute, publicly display or
# publicly perform the software or other information provided herein,
# nor is Licensee permitted to decompile, reverse engineer, or
# disassemble the software. Usage of the software and other information
# provided by Altair(or its resellers) is only as explicitly stated in the
# applicable end user license agreement between Altair and Licensee.
# In the absence of such agreement, the Altair standard end user
# license agreement terms shall govern.

"""
This hook is intended to run pbs_est.
"""
import os
import subprocess
import time

import pbs

pbs_est_cmd = os.path.join(pbs.pbs_conf['PBS_EXEC'], 'sbin', 'pbs_est')

e = pbs.event()

pbs.logmsg(pbs.LOG_DEBUG, "Starting job start time estimation task")

simtime = os.path.join(pbs.pbs_conf['PBS_EXEC'], 'lib')

if 'LD_LIBRARY_PATH' in os.environ:
    env = f'{os.environ["LD_LIBRARY_PATH"]}:{simtime}'
else:
    env = simtime

os.environ['LD_LIBRARY_PATH'] = env

try:
    p = subprocess.Popen([pbs_est_cmd])
    p.wait()
except KeyboardInterrupt:
    p.kill()
    e.reject("hook alarm while executing %s" % pbs_est_cmd)

if p.returncode != 0:
    e.reject("%s exited abnormally with return code %d" %
             (pbs_est_cmd, p.returncode))
else:
    e.accept()
