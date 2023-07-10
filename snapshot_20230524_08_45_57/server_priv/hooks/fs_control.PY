""" Filesystem control
Control of filesystems
targeted towards QUEUEJOB and MODIFYJOB events

If jobs do not request filesystems, make job request all
smooth out syntax for fs request.

Preserve specified filesystems.

smoother filesystems syntax strip out and expand

global filesystems: match short name
add to setting list.

"""

import traceback
from typing import List, Tuple
import pbs

event = pbs.event()
server = pbs.server()


def extract_req_val(resource_list: str, key: str) -> str:
    '''Given a pbs.Resource_List as a string, extract a target
    key's information

    '''
    for resource in resource_list.split(","):
        if resource.split("=")[0] == key:
            return resource.split("=", maxsplit=1)[1]
    return ""


def get_fs_attr_names(valid_fs_list: List[str], req_fs: str) -> Tuple[List[str], List[str], List[str]]:
    '''find our filesystem identifiers that match user requested
    filesystems.  Match the starting fs'''
    fs_names = []
    fs_invalid = []
    fs_ambiguous = []
    for fs in req_fs.split(":"):
        count = 0
        found = False
        for valid_fs in valid_fs_list:
            if valid_fs.startswith(fs):
                found = True
                fs_names.append(valid_fs)
                count += 1
                if count > 1:
                    fs_ambiguous.append(fs)
                    break
        if not found:
            fs_invalid.append(fs)

    return fs_names, fs_invalid, fs_ambiguous


try:
    # find filesystems in Resource_List
    # Defaults for filesystems are inherited from server setting so not set here
    resource_list = str(event.job.Resource_List)
    valid_fs = str(server.resources_available["valid_filesystems"])
    valid_fs_list = valid_fs.split(",")
    filesystems_str = extract_req_val(resource_list, "filesystems")
    fs_attr_names = []
    if filesystems_str:
        if event.type == pbs.MODIFYJOB:
            # We're modifying filesystems, clear any that may currently be set.
            for name in valid_fs_list:
                event.job.Resource_List[name] = None
        fs_attr_names, fs_invalid, fs_ambiguous = get_fs_attr_names(valid_fs_list, filesystems_str)
        if fs_ambiguous:
            ambiguous_matches = []
            for fs in fs_ambiguous:
                ambiguous_matches.extend([fs_name for fs_name in valid_fs_list if fs_name.startswith(fs)])
            event.reject(f"Ambiguous filesystem identifier: {', '.join(fs_ambiguous)}, matches: {', '.join(ambiguous_matches)}")
        if fs_invalid:
            event.reject(f"Invalid filesystem identifiers {', '.join(fs_invalid)}")
    else:
        # unspecified gets all filesystems
        if event.type == pbs.QUEUEJOB:
            fs_attr_names = valid_fs_list
    for name in fs_attr_names:
        event.job.Resource_List[name] = True
    event.accept()

except Exception as ex:
    pbs.logmsg(pbs.LOG_ERROR, f"Exception: {ex}\n{str(traceback.format_exc().strip().splitlines())}")
    event.reject(f"Exception in {event.hook_name} hook encountered.  Please contact your administrator.")
