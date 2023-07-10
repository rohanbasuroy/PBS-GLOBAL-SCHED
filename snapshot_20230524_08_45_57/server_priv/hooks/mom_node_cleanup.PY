# 
# Do basic cleanup tasks during the prologue and epilogue
# TODO: Figure out where the shell scripts are going to live. 
# TODO: e.type is probably just a number, see if we can get an event name
#
import pbs
import subprocess

try:
    e = pbs.event()
    j = e.job
    jobid = str(j.jobid)
    username = str(j.euser)

    if e.type == pbs.EXECJOB_PROLOGUE:
       op = "-p"
       which = "EXECJOB_PROLOGUE"
    elif e.type == pbs.EXECJOB_EPILOGUE:
       op = "-e"
       which = "EXECJOB_EPILOGUE"
    else:
       e.reject(f"The cleanup script should only be called from the execjob_[prologue|epilogue] events;  It was called from {e.type}")

#    tool = "clean-node.sh"
    tool = "/usr/bin/logger"
    subprocess.run([tool, op, jobid, username], timeout = 10, shell = False)
    e.accept()
except Exception as ex:
   e.reject(f"hook_job_cleanup.py threw an exception on jobid {jobid} for user {username}: {ex}")
