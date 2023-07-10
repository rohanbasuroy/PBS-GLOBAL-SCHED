import pbs
import subprocess

REJECT = False
try:
    e = pbs.event()
    j = e.job
    jobid = j.id
    u = j.euser

    if e.type == pbs.EXECJOB_BEGIN:
       op = "EXECJOB_BEGIN"
       tool = "/soft/pbs/scripts/prologue.sh"
    elif e.type == pbs.EXECJOB_END:
       op = "EXECJOB_END"
       tool = "/soft/pbs/scripts/epilogue.sh"
    else:
       op = "UNKNOWN"
       tool = None

    if tool:
        # get resources
        resource_list = pbs.server().job(pbs.event().job.id).Resource_List
        resources = ';'.join( [f"{x}={resource_list[x]}" for x in resource_list.keys()] )
        message = f"PBS_MOM_Hook:{op}:Job:{jobid}:User:{u}:Resources:{resources}"
        pbs.logjobmsg(jobid, f"{tool} {message}")
        completed_proc = subprocess.run([tool, message], timeout = 120, shell = False)
        # check script return code and offline+reject
        if completed_proc.returncode != 0:
            local_node = pbs.get_local_nodename()
            vnl = pbs.event().vnode_list
            current_state = pbs.server().vnode(local_node).state
            if current_state != pbs.ND_OFFLINE:
                vnl[local_node].state = pbs.ND_OFFLINE
                vnl[local_node].comment = f"(pro|epi)logue failure during {op}.  offlining node..."
                REJECT = True
except Exception as ex:
    pbs.logmsg(pbs.LOG_DEBUG, f"hook_mom_logue_launcher.py threw exception: {ex}")
finally:
    # reject request if REJECT flag is set, otherwise accept
    if REJECT:
        e.reject( "(pro|epi)logue failure..." )
    else:
        e.accept()
