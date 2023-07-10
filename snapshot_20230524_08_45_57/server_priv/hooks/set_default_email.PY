"""Set Default Email

Pulls a default email out of pw_gecos so that users get a sane default

This hook is meant for queuejob use

"""

import traceback
import pwd
import pbs

event = pbs.event()
server = pbs.server()

try:
    full_username = event.requestor
    # Sometimes we get these as user@host, so strip host since we're also using flatuid
    end_pos = full_username.find('@')
    username = full_username[:end_pos] if end_pos != -1 else full_username

    if event.job.Mail_Users is None:
        # User email shouud be last of a ',' separated field.
        gecos_email = pwd.getpwnam(username).pw_gecos.split(',')[-1]
        event.job.Mail_Users = pbs.email_list(gecos_email)

    event.accept()

except Exception as ex:
    pbs.logmsg(pbs.LOG_ERROR, f"Exception: {ex}\n{str(traceback.format_exc().strip().splitlines())}")
    event.reject(f"Exception in {event.hook_name} hook encountered.  Please contact your administrator.")
