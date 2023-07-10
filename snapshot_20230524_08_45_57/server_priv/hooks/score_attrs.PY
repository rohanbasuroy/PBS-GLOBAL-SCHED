"""Add attributes to a job for score function usage

This is intended for use with queuejob, modifyjob, and movejob

"""
import json
import traceback
from typing import Any, Dict, List
import pbs


default_config = {"project_list_path": "/etc/projects.list",
                  "default_project_priority": 1.0,
                  }


# If event for server fails, then there is something really badly broken in
# PBS itself
event = pbs.event()
server = pbs.server()


def get_config(default_config: Dict[str, Any]) -> Dict:
    config = {}
    config.update(default_config)
    if pbs.hook_config_filename is not None:
        with open(pbs.hook_config_filename) as f:
            config.update(json.loads(f.read()))
    extra_keys = set(config.keys()) - set(default_config.keys())
    if extra_keys:
        raise ValueError(f"Invalid keys found: {extra_keys}")
    return config


def add_attrs(event: Any, server: Any) -> None:
    '''Add attributes for the score function.
        project_factor
    '''
    config = get_config(default_config)
    resource_list = event.job.Resource_List
    if event.type == pbs.MODIFYJOB and event.job.Account_Name is not None:
        resource_list["project_priority"] = fetch_priority(event.job_o.Account_Name, config)
    else:
        resource_list["project_priority"] = fetch_priority(event.job.Account_Name, config)
    # Requeue undoes boost?  Do we want to differentiate between user and admin here?
    #resource_list["score_boost"] = 0.0
    #resource_list["total_cpus"] = float(server.resources_default["total_cpus"])
    # ncpus doesn't need to be handled here: PBS does the math correctly internally
    # if the "static" portion was being precomputed, we'd have to replicate that here
    event.accept()


def fetch_priority(project_name: str, config: Dict[str, Any]) -> float:
    '''return a score from our valid projects if we can find one.'''
    project_priority = config["default_project_priority"]
    if project_name is not None:
        try:
            with open(config["project_list_path"]) as f:
                try:
                    for line in f:
                        sp_line = line.split('#')[0].split('=')
                        if project_name.lower() == sp_line[0].lower():
                            project_priority = float(sp_line[1])
                            break
                except Exception as err:
                    pbs.logmsg(pbs.LOG_WARNING, f"Failure in reading project priorities: {err}")
                    pbs.logmsg(pbs.LOG_WARNING, f"Setting project_priority to default: {project_priority}")
        except IOError:
            pbs.logmsg(pbs.LOG_WARNING, f"Unable to open {config['project_list_path']}")
            pbs.logmsg(pbs.LOG_WARNING, f"Setting project_priority to default: {project_priority}")

    return project_priority


try:
    add_attrs(event, server)
except Exception as ex:
    pbs.logmsg(pbs.LOG_ERROR, f"Exception: {ex}\n{str(traceback.format_exc().strip().splitlines())}")
    event.reject(f"Exception in {event.hook_name} hook encountered.  Please contact your administrator.")
