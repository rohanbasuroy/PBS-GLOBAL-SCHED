# 
# Add job start (EXECJOB_PROLOGUE) and job end (EXECJOB_EPILOGUE) events
# to syslog to avoid having to go to the PBS accounting logs.
#
import pbs
import subprocess

try:
    e = pbs.event()
    j = e.job
    jobid = j.id

    if e.type == pbs.EXECJOB_PROLOGUE:
       op = "EXECJOB_PROLOGUE"
    elif e.type == pbs.EXECJOB_EPILOGUE:
       op = "EXECJOB_EPILOGUE"
    else:
       op = "UNKNOWN"

    message = f'"PBS_MOM_Hook:{op}:Job:{jobid}"'
    tool = "/usr/bin/logger"
    pbs.logjobmsg(jobid, f"{tool} {message}")
    subprocess.run([tool, message], timeout = 10, shell = False)
except Exception as ex:
    pbs.logmsg(pbs.LOG_DEBUG, f"hook_job_info_syslog.py threw exception: {ex}")  
finally:
    # This is a convenience logging function, it should always accept
    e.accept()
