import os
import sys
import pandas as pd
import glob
import numpy as np
import re
import time
import collections

def read_csv_files(file_pattern):
    all_files = glob.glob(file_pattern)
    df_list = []
    for file in all_files:
        df = pd.read_csv(file)
        df_list.append(df)
    return pd.concat(df_list)

def sys_information(sc, fields, job_fields):
    sc=sc.upper()
    node=[]
    name=[]
    with open("./data/system_information", 'r') as file:
        for line in file:
            supercomputer, nodes = line.strip().split(':')
            name.append(supercomputer)
            node.append(nodes)
    if sc not in name:
        nodes = input(f"Number of nodes for {sc}: ")
        with open("./data/system_information", 'a') as file:
            file.write(f"{sc}:{nodes}\n")
        name.append(sc)
        node.append(nodes)
    ind_queue=np.where(fields == "QUEUE_NAME")[0][0]
    all_queues=[]
    for i in range(len(job_fields)):
        if job_fields[i][ind_queue] not in all_queues:
            all_queues.append(job_fields[i][ind_queue])
    all_queues_num=[0 for i in range(len(all_queues))]
    for i in range(len(job_fields)):
        all_queues_num[all_queues.index(job_fields[i][ind_queue])]+=1
    return node[name.index(sc)], all_queues, all_queues_num

def get_jobs_between_timeframe(df, start_time, end_time):
    jobs_between_timeframe = df[((df['QUEUED_TIMESTAMP'] >= start_time) | (df['START_TIMESTAMP'] >= start_time)) & ((df['QUEUED_TIMESTAMP'] <= end_time))]
    job_fields = jobs_between_timeframe.to_numpy()
    fields = jobs_between_timeframe.columns.to_numpy()
    return fields, job_fields
    
def request_check_and_get_data():
    available_servers = [item for item in os.listdir("./data") if os.path.isdir(os.path.join("./data", item))]
    if machine_name not in available_servers:
        print("Machine data not available. Please create a directory inside './data' with the machine name and extract the DIM_JOB_COMPOSITE csv files of the machine inside the directory.")
        sys.exit()
    else:
        print("Machine information available!")
        file_pattern = "./data/"+machine_name+"/ANL-ALCF-DJC-"+machine_name.upper()+"_*.csv"
        df = read_csv_files(file_pattern)
        fields, job_fields = get_jobs_between_timeframe(df, start_timeframe, end_timeframe)
        if len(job_fields)==0:
            print("Fix the start and/or end time frame. Either no job exist in between the set timeframes, or no job log available in the set timeframes.")
            sys.exit()
        else:
            print("Job log available between the start and end timeframes!")
    return fields, job_fields

def print_info_and_set_priorities(node_num, all_queues, all_queues_num):
    print(machine_name.upper() + " has "+str(node_num)+ " nodes.")
    queue_priorities=[1 for i in range(len(all_queues))]
    reserve_nodes=[0 for i in range(len(all_queues))]
    if set_priorities == 0:
        print("The system has the following queues, all with equal scheduling priorities.")
        for i in range(len(all_queues)):
            print("Queue Name: "+str(all_queues[i])+ " has "+str(all_queues_num[i])+" number of jobs submitted.")
    else:
        qn = re.findall(r'([a-zA-Z0-9-]+):', set_priorities)
        pr = re.findall(r':(\d+)', set_priorities)
        pr=[float(i) for i in pr]
        for i in range(len(qn)):
            queue_priorities[all_queues.index(qn[i])] = pr[i]        
    if set_reservations == 0:
         print("No reservation made for specific queues.")
    else:
        qn = re.findall(r'([a-zA-Z0-9-]+):', set_reservations)
        pr = re.findall(r':(\d+)', set_reservations)
        pr=[float(i) for i in pr]
        for i in range(len(qn)):
            reserve_nodes[all_queues.index(qn[i])] = pr[i]  
        if sum(reserve_nodes)>float(node_num):
            print("Reserved nodes greater than the nodes available in the system. Removing all reservations.")
            reserve_nodes=[0 for i in range(len(all_queues))]
    return queue_priorities, reserve_nodes

# def simulate_execution(in_all_queue_list, user_selected_attributes, user_selected_weights, fields, machine_state, machine_state_index, current_timestamp):
#     try:
#         sorted_jobs = sorted(in_all_queue_list, key=lambda x: [sum(float(x[fields.index(attr)]) * weight if attr in fields else 0 for attr, weight in zip(user_selected_attributes, user_selected_weights))])
#     except:
#         print("The selected attributes can not be sorted to determine job scheduling sequence.")
#         sys.exit()    
#     start_index = machine_state_index.index(current_timestamp)
#     end_index = machine_state_index.index(current_timestamp+scheduling_interval)
#     while start_index <= end_index:
#         sched_indices=[]
#         for i in range(len(sorted_jobs)):
#             if machine_state[start_index] + int(sorted_jobs[i][fields.index("NODES_REQUESTED")]) <= int(node_num):
#                 print(sorted_jobs[i][fields.index('JOB_NAME')] + " Job scheduled")
#                 for j in range(int(sorted_jobs[i][fields.index("RUNTIME_SECONDS")])):
#                     machine_state[start_index+j]+=int(sorted_jobs[i][fields.index("NODES_REQUESTED")])
#                 sched_indices.append(i)
#             else:
#                 break
#         for s in sched_indices:
#             del sorted_jobs[s]
#         start_index+=1
#     return machine_state, machine_state_index, sorted_jobs
                        
# def simulate_enqueue(node_num, fields, job_fields, user_selected_attributes, user_selected_weights):
#     fields=fields.tolist()
#     job_fields=job_fields.tolist()
#     job_fields = sorted(job_fields, key=lambda x: x[fields.index('QUEUED_TIMESTAMP')])
#     node_num=float(node_num)
#     unix_loc = [i for i in range(len(fields)) if "TIMESTAMP" in fields[i]]
#     job_fields = [[int(time.mktime(time.strptime(job_fields[i][j], "%Y-%m-%d %H:%M:%S"))) if j in unix_loc else job_fields[i][j] for j in range(len(job_fields[i]))] for i in range(len(job_fields))]
#     start_timestamp=int(time.mktime(time.strptime(start_timeframe, "%Y-%m-%d %H:%M:%S")))
#     end_timestamp=int(time.mktime(time.strptime(end_timeframe, "%Y-%m-%d %H:%M:%S")))
    
#     machine_state = [0 for i in range(start_timestamp, int(1.5*end_timestamp))]  ##
#     machine_state_index = [i for i in range(start_timestamp, int(1.5*end_timestamp))] ##
    
#     current_timestamp=start_timestamp
#     done=False
    
#     print(start_timestamp, end_timestamp)
    
#     in_all_queue_list=[]
#     while done!=True:
#         queue_index=[]
#         for i in range(len(job_fields)):
#             if job_fields[i][fields.index("QUEUED_TIMESTAMP")] <= current_timestamp:
#                 in_all_queue_list.append(job_fields[i])
#                 queue_index.append(i)
#             else:
#                 break
#         for i in queue_index:
#             del job_fields[i]
#         if len(in_all_queue_list)==0:
#             pass
#         else:
#             machine_state, machine_state_index, in_all_queue_list = simulate_execution(in_all_queue_list, user_selected_attributes, user_selected_weights, fields, machine_state, machine_state_index, current_timestamp)   
#         current_timestamp += scheduling_interval
#         print(current_timestamp, len(job_fields))
#         if len(job_fields)==0:
#             done=True
#     for job in in_all_queue_list:
#         print(str(job[0])+ " could not start execution within the set start and end timeframes.")




def simulate_execution(in_all_queue_list, user_selected_attributes, user_selected_weights, fields, machine_state, machine_state_index, current_timestamp):    
    try:
        min_vals = {}
        max_vals = {}
        for attr in user_selected_attributes:
            attr_values = [float(in_all_queue_list[j][fields.index(attr)]) for j in range(len(in_all_queue_list))]
            min_vals[attr] = min(attr_values)
            max_vals[attr] = max(attr_values)
        sorted_jobs = sorted(in_all_queue_list, key=lambda x: [sum(((float(x[fields.index(attr)]) - min_vals[attr]) / (max_vals[attr] - min_vals[attr])) * weight if attr in fields else 0 for attr, weight in zip(user_selected_attributes, user_selected_weights))])  
    except:
        print("The selected attributes cannot be sorted to determine job scheduling sequence.")
        sys.exit()
        
    start_index = machine_state_index.index(current_timestamp)
    end_index = machine_state_index.index(current_timestamp + scheduling_interval)
    
    sorted_jobs_deque = collections.deque(sorted_jobs)
    sched_indices = []
    
    while start_index <= end_index:
        for i in range(len(sorted_jobs_deque)):
            job = sorted_jobs_deque[i]
            nodes_requested = int(job[fields.index("NODES_REQUESTED")])
            
            if machine_state[start_index] + nodes_requested <= int(node_num):
                print(job[fields.index('JOB_NAME')] + " Job scheduled at a unix timestamp of " + str(machine_state_index[start_index]) +
                      ". It runs on "+  str(job[fields.index('NODES_REQUESTED')]) + " nodes, and finishes execution at a unix timestamp of "
                      +  str(int(machine_state_index[start_index])+ int(job[fields.index('NODES_REQUESTED')])))
                for j in range(int(job[fields.index("RUNTIME_SECONDS")])):
                    machine_state[start_index + j] += nodes_requested
                sched_indices.append(i)
                
            else:
                break
        
        for s in sorted(sched_indices, reverse=True):
            sorted_jobs_deque.remove(sorted_jobs_deque[s])
            
        start_index += 1
        sched_indices = []
        
    return machine_state, machine_state_index, list(sorted_jobs_deque)


def simulate_enqueue(node_num, fields, job_fields, user_selected_attributes, user_selected_weights):
    fields = fields.tolist()
    job_fields = job_fields.tolist()
    job_fields = sorted(job_fields, key=lambda x: x[fields.index('QUEUED_TIMESTAMP')])
    node_num = float(node_num)
    
    unix_loc = [i for i in range(len(fields)) if "TIMESTAMP" in fields[i]]
    job_fields = [[int(time.mktime(time.strptime(job_fields[i][j], "%Y-%m-%d %H:%M:%S"))) if j in unix_loc else job_fields[i][j] for j in range(len(job_fields[i]))] for i in range(len(job_fields))]
    
    start_timestamp = int(time.mktime(time.strptime(start_timeframe, "%Y-%m-%d %H:%M:%S")))
    end_timestamp = int(time.mktime(time.strptime(end_timeframe, "%Y-%m-%d %H:%M:%S")))
    
    machine_state = [0 for i in range(start_timestamp, int(1.5 * end_timestamp))] ##
    machine_state_index = [i for i in range(start_timestamp, int(1.5 * end_timestamp))] ##
    
    current_timestamp = start_timestamp
    done = False
    
    in_all_queue_list = []
    
    while not done:
        queue_index = []
        
        for i in range(len(job_fields)):
            if job_fields[i][fields.index("QUEUED_TIMESTAMP")] <= current_timestamp:
                in_all_queue_list.append(job_fields[i])
                queue_index.append(i)
            else:
                break
        
        for i in reversed(queue_index):
            del job_fields[i]
        
        if len(in_all_queue_list) > 0:
            machine_state, machine_state_index, in_all_queue_list = simulate_execution(in_all_queue_list, user_selected_attributes, user_selected_weights, fields, machine_state, machine_state_index, current_timestamp)
        
        current_timestamp += scheduling_interval
        
        
        if len(job_fields) == 0:
            done = True
    
    for job in in_all_queue_list:
        print(str(job[0]) + " could not start execution within the set start and end timeframes.")

    
            
if __name__ == "__main__":
    machine_name = str(sys.argv[1]) ##
    #start_timeframe = "2019-12-10 19:00:00" ##
    #end_timeframe = "2019-12-14 19:00:00" ##
    start_timeframe = str(sys.argv[2])+ " "+str(sys.argv[3])
    end_timeframe = str(sys.argv[4])+ " "+str(sys.argv[5])
    set_priorities = "debug-flat-quad:2, analysis:4"##
    set_reservations = "debug-flat-quad:5, analysis:10" ##
    user_selected_attributes = ["QUEUED_TIMESTAMP", "WALLTIME_SECONDS"]
    user_selected_weights = [2,1]
    scheduling_interval = 300 ##
    fields, job_fields = request_check_and_get_data()
    node_num, all_queues, all_queues_num = sys_information(machine_name, fields, job_fields)
    queue_priorities, reserve_nodes = print_info_and_set_priorities(node_num, all_queues, all_queues_num)
    simulate_enqueue(node_num, fields, job_fields, user_selected_attributes, user_selected_weights)
    