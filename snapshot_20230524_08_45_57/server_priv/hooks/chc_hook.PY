#  Copyright (c) 2018 Hewlett Packard Enterprise Development LP
#  All rights reserved.
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307 USA
#

import pbs
import sys
import subprocess
from subprocess import Popen
from subprocess import PIPE
import os

# To run health checks on pbs nodes:
# create hook chc_hook
# set hook chc_hook event = execjob_prologue,execjob_epilogue,exechost_startup (To run this hook prior to and post launching any job as well as when pbs_mom is started/restarted)
# set hook chc_hook enabled = true
# import hook chc_hook application/x-python default /opt/clustertest/bin/pbs_chc.py
# One can also optionally add exechost_periodic to the hook event


# ACTION="WARN"(default) -- will log details of the health check failure if any to mom_logs and allow the job to be placed on the nodes.
# ACTION="OFFLINE" -- will mark the node offline on occurence of any health check failure.
# ACTION="REBOOT" -- will offline and reboot the node on occurence of any health check failure. This will also requeue the current job.
# Note: A hook with its user attribute set to 'pbsuser' CANNOT use ACTION="REBOOT".
#       A hook with event=exechost_periodic CANNOT use ACTION="REBOOT".

ACTION = "OFFLINE"

e=pbs.event()
nodename = ""
vnl = e.vnode_list

def getNodeName():
    nodename = pbs.get_local_nodename()
    return nodename

def warnFailure(stdout):
    #Log information on the failed health check
    if (sys.version_info.major == 3):
        output = stdout.decode('utf-8').replace("\n"," ")
        pbs.logmsg(pbs.EVENT_DEBUG,"CHC- " + output)
    else:
        pbs.logmsg(pbs.EVENT_DEBUG, "'%s'" % (str(stdout).replace("\n", " ")))

def markNodeOffline(stdout):
    nodename = getNodeName()
    #Log information on the failed health check
    if (sys.version_info.major == 3):
        output = stdout.decode('utf-8').replace("\n"," ")
        pbs.logmsg(pbs.EVENT_DEBUG,"CHC- " + output)
    else:
        pbs.logmsg(pbs.EVENT_DEBUG, "'%s'" % (str(stdout).replace("\n", " ")))

    pbs.logmsg(pbs.EVENT_DEBUG,"CHC- Offline node: %s"%(nodename))
    vnl[nodename].state = pbs.ND_OFFLINE
    vnl[nodename].comment =  "CHC- Offlined due to node health check failure"

def nodeReboot(stdout):
    nodename = getNodeName()
    #Log information on the failed health check
    if (sys.version_info.major == 3):
        output = stdout.decode('utf-8').replace("\n"," ")
        pbs.logmsg(pbs.EVENT_DEBUG,"CHC- " + output)
    else:
        pbs.logmsg(pbs.EVENT_DEBUG, "'%s'" % (str(stdout).replace("\n", " ")))

    pbs.logmsg(pbs.EVENT_DEBUG,"CHC- Reboot and Offline node: %s"%(nodename))
    vnl[nodename].state = pbs.ND_OFFLINE
    vnl[nodename].comment =  "CHC- Mom host offlined and rebooting due to node health check failure"
    pbs.event().job.rerun()
    pbs.reboot()

def markNodeOnline():
    nodename = getNodeName()
    pbs.logmsg(pbs.EVENT_DEBUG,"CHC- Online node: %s"%(nodename))
    vnl[nodename].state = pbs.ND_FREE
    vnl[nodename].comment = None


try:

    script = "/pe/nhc/bin/chc_scheduler"

    #Get the name of the node where this hook runs
    nodename = getNodeName()
    pbs.logmsg(pbs.EVENT_DEBUG,"CHC- Node Health Check started on "+nodename)

    #Runs health checks on pbs nodes
    proc = subprocess.Popen(script,stdout=subprocess.PIPE,stderr=subprocess.PIPE,shell=True)
    stdout, stderr = proc.communicate()
    result=proc.returncode

    #If NHC health checks fail
    if (result != 0):

        if (ACTION == "WARN"):
            warnFailure(stdout)

        if (ACTION == "OFFLINE"):
            markNodeOffline(stdout)
            raise Exception("Node Health Check failed")

        if (ACTION == "REBOOT"):
            nodeReboot(stdout)



    elif (result == 0):
        pbs.logmsg(pbs.EVENT_DEBUG,"CHC- Node Health Check passed on "+nodename)

#        #Mark the node online, previously offlined due to health check failure
#        vnode = pbs.server().vnode(nodename)
#        if not vnode.comment is None:
#            if (vnode.comment.startswith('CHC-')):
#                markNodeOnline()


except SystemExit:
    pass

except:
    #Reject the event on hook failure
    pbs.event().reject("CHC- %s hook failed with %s." % (e.hook_name,  sys.exc_info()[:2]))
