import pbs
import os
import subprocess
from functools import reduce

REJECT = False
REJECT_MSG = ""
# server resources to pass to the script
SERVER_FS_RESOURCES=('grand_fs','eagle_fs','swift_fs')
try:
    # event, job obj, jobid, user
    e = pbs.event()
    if e.type not in [pbs.EXECHOST_PERIODIC,]:
        j = e.job
        jobid = j.id
        u = j.euser
    # server and server/job resource lists
    server = pbs.server()
    server_resource_list = server.resources_available
    if e.type not in [pbs.EXECHOST_PERIODIC,]:
        resource_list = server.job(jobid).Resource_List

    if e.type == pbs.RUNJOB:
        op = "RUNJOB"
        tool = ""
    elif e.type == pbs.EXECHOST_PERIODIC:
        op = "EXECHOST_PERIODIC"
        tool = "/soft/pbs/scripts/process_cleanup.sh"
    elif e.type == pbs.EXECJOB_BEGIN:
       op = "EXECJOB_BEGIN"
       tool = "/soft/pbs/scripts/prologue.sh"
    elif e.type == pbs.EXECJOB_END:
       op = "EXECJOB_END"
       tool = "/soft/pbs/scripts/epilogue.sh"
    else:
       op = "UNKNOWN"
       tool = None

    if op == "RUNJOB":
        # mount sanity check
        if not reduce( bool.__and__, [server_resource_list[k] for k in resource_list.keys() if ((k in SERVER_FS_RESOURCES) and (resource_list[k] == True)) ], True ):
            # user requested an fs that was downed on the server side
            # reject and place job on hold
            j.Hold_Types = pbs.hold_types("u")
            REJECT_MSG = "one or more filesystems requested by this job are currently offline"
            REJECT = True
    elif op == "EXECHOST_PERIODIC":
        remaining_procs_file = "/var/tmp/pbsjob_remaining_procs"
        # first check for remaining procs file(before hitting the server up for node state)
        if os.path.isfile(remaining_procs_file):
            local_node = pbs.get_local_nodename()
            vnode = e.vnode_list[local_node]
            current_state = server.vnode(local_node).state
            # check node state
            #if current_state == pbs.ND_OFFLINE:
            if (current_state & int(pbs.ND_OFFLINE)) and not (current_state & int(pbs.ND_DOWN)):
                # execute script
                message = open(remaining_procs_file, 'r').read().strip()
                completed_proc = subprocess.run([tool, message], timeout = 120, shell = False)
                # check rc
                if completed_proc.returncode == 0:
                    # cleanup completed successfully.  delete remaining_procs_file and return node to service
                    os.remove(remaining_procs_file)
                    vnode.comment = " "
                    vnode.state = int( pbs.ND_STATE_FREE )
                    pbs.logmsg(pbs.LOG_DEBUG, f"{tool} script: {vnode.name} onlined following successful cleanup")
                else:
                    # cleanup failed; keep trying...
                    pbs.logmsg(pbs.LOG_DEBUG, f"{tool} script: {vnode.name} failed cleanup.  continuing...")
            else:
                pbs.logmsg(pbs.LOG_DEBUG, f"{tool} script: {vnode.name} is not offline, but a remaining_procs file exists...")
    elif op in ("EXECJOB_BEGIN","EXECJOB_END"):
        # execjob_* specific vars
        local_node = pbs.get_local_nodename()
        vnl = e.vnode_list
        # check /soft mount
        if not os.path.ismount('/soft'):
            current_state = server.vnode(local_node).state
            if current_state != pbs.ND_OFFLINE:
                vnl[local_node].state = pbs.ND_OFFLINE
                vnl[local_node].comment = f"{op}: /soft is not mounted"
                REJECT = True
        # run script
        else:
            # get resources
            resources = ';'.join( [f"{x}={resource_list[x]}" for x in resource_list.keys()] )
            server_resources = ';'.join( [f"server_{x}={server_resource_list[x]}" for x in server_resource_list.keys() if x in SERVER_FS_RESOURCES] )
            if server_resources: resources += ';' + server_resources
            # script args
            message = f"PBS_MOM_Hook:{op}:Job:{jobid}:User:{u}:Resources:{resources}"
            pbs.logjobmsg(jobid, f"{tool} {message}")
            # execute script
            completed_proc = subprocess.run([tool, message], timeout = 120, shell = False)
            pbs.logjobmsg(jobid, f"{tool} script completed with return code {completed_proc.returncode}")
            # check script return code and offline+reject
            if completed_proc.returncode != 0:
                current_state = server.vnode(local_node).state
                if current_state != pbs.ND_OFFLINE:
                    vnl[local_node].state = pbs.ND_OFFLINE
                    # update node status with error message saved by script(if available)
                    if os.path.isfile('/var/tmp/logue_firstfail'):
                        firstfail_msg = subprocess.check_output( [ "cat", "/var/tmp/logue_firstfail" ] ).strip().decode()
                        vnl[local_node].comment = f"{op}: {firstfail_msg}"
                    else:
                        vnl[local_node].comment = f"{op}: offlining node..."
                    REJECT = True
            # reject job due to process cleanup failure
            # NOTE: does NOT offline the node
            elif (op in ("EXECJOB_BEGIN",)) and os.path.isfile('/var/tmp/pbsjob_remaining_procs'):
                REJECT = True
except Exception as ex:
    pbs.logmsg(pbs.LOG_DEBUG, f"hook_mom_logue_launcher.py threw exception: {ex}")
finally:
    # reject request if REJECT flag is set, otherwise accept
    if REJECT:
        if REJECT_MSG:
            msg = f"(pro|epi)logue failure:  {REJECT_MSG}"
        else:
            msg = f"(pro|epi)logue failure"
        e.reject( msg )
    else:
        e.accept()
