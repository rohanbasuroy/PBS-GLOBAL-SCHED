import os
import sys
import pandas as pd
import glob
import numpy as np
import re
import time
import collections
import argparse

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
        ss="Machine data not available. Please create a directory inside './data' with the machine name and extract the DIM_JOB_COMPOSITE csv files of the machine inside the directory."
        print(ss)
        output_list.append(ss)
        sys.exit()
    else:
        ss="Machine information available!"
        print(ss)
        output_list.append(ss)
        file_pattern = "./data/"+machine_name+"/ANL-ALCF-DJC-"+machine_name.upper()+"_*.csv"
        df = read_csv_files(file_pattern)
        fields, job_fields = get_jobs_between_timeframe(df, start_timeframe, end_timeframe)
        if len(job_fields)==0:
            ss="Fix the start and/or end time frame. Either no job exist in between the set timeframes, or no job log available in the set timeframes."
            print(ss)
            output_list.append(ss)
            sys.exit()
        else:
            ss="Job log available between the start and end timeframes!"
            print(ss)
            output_list.append(ss)
    return fields, job_fields

def print_info_and_set_priorities(node_num, all_queues, all_queues_num):
    ss=machine_name.upper() + " has "+str(node_num)+ " nodes."
    print(ss)
    output_list.append(ss)
    queue_priorities=[1 for i in range(len(all_queues))]
    reserve_nodes=[0 for i in range(len(all_queues))]
    if set_priorities == 0:
        ss="The system has the following queues, all with equal scheduling priorities."
        print(ss)
        output_list.append(ss)
        for i in range(len(all_queues)):
            ss="Queue Name: "+str(all_queues[i])+ " has "+str(all_queues_num[i])+" number of jobs submitted."
            print(ss)
            output_list.append(ss)
    else:
        qn = re.findall(r'([a-zA-Z0-9-]+):', set_priorities)
        pr = re.findall(r':(\d+)', set_priorities)
        pr=[float(i) for i in pr]
        for i in range(len(qn)):
            try:
                queue_priorities[all_queues.index(qn[i])] = pr[i]  
            except:
                pass
    if set_reservations == 0:
         ss="No reservation made for specific queues."
         print(ss)
         output_list.append(ss)
    else:
        qn = re.findall(r'([a-zA-Z0-9-]+):', set_reservations)
        pr = re.findall(r':(\d+)', set_reservations)
        pr=[float(i) for i in pr]
        for i in range(len(qn)):
            try:
                reserve_nodes[all_queues.index(qn[i])] = pr[i] 
            except:
                pass
        if sum(reserve_nodes)>float(node_num):
            ss="Reserved nodes greater than the nodes available in the system. Removing all reservations."
            print(ss)
            output_list.append(ss)
            reserve_nodes=[0 for i in range(len(all_queues))]
    return queue_priorities, reserve_nodes

def simulate_execution(in_all_queue_list, user_selected_attributes, user_selected_weights, fields, machine_state, machine_state_index, current_timestamp, machine_state_reserved):    
    try:
        min_vals = {}
        max_vals = {}
        all_attr_list=[]
        for attr in user_selected_attributes:
            attr_values = [float(in_all_queue_list[j][fields.index(attr)]) for j in range(len(in_all_queue_list))]
            min_vals[attr] = min(attr_values)
            max_vals[attr] = max(attr_values)
            attr_values = [(i-min_vals[attr])/(max_vals[attr]-min_vals[attr]) for i in attr_values]
            all_attr_list.append(attr_values)
        combined_attr_val=[]
        for j in range(len(all_attr_list[0])):
            val=0
            for i in range(len(user_selected_weights)):
                val+=all_attr_list[i][j]*user_selected_weights[i]
            val+=queue_priorities[all_queues.index(in_all_queue_list[j][fields.index('QUEUE_NAME')])]
            combined_attr_val.append(val)
        sorted_jobs = [x for _,x in sorted(zip(combined_attr_val,in_all_queue_list))] 
    except:
        ss="The selected attributes cannot be sorted to determine job scheduling sequence."
        print(ss)
        output_list.append(ss)
        sys.exit()      
    start_index = machine_state_index.index(current_timestamp)
    end_index = machine_state_index.index(current_timestamp + scheduling_interval)  
    sorted_jobs_deque = collections.deque(sorted_jobs)
    sched_indices = []  
    while start_index <= end_index:
        for i in range(len(sorted_jobs_deque)):
            job = sorted_jobs_deque[i]
            nodes_requested = int(job[fields.index("NODES_REQUESTED")])
            if job[fields.index("QUEUE_NAME")] in reserved_queue_name:
                res_ind=reserved_queue_name.index(job[fields.index("QUEUE_NAME")])
                if nodes_requested > int(reserved_queue_nodes[res_ind]):
                    ss=job[fields.index('JOB_NAME')] + " cannot be scheduled due to insufficient nodes reserved for the queue."
                    print(ss)
                    output_list.append(ss)
                    sched_indices.append(i)
                    break
                elif machine_state_reserved[res_ind][start_index] + nodes_requested <= int(reserved_queue_nodes[res_ind]):
                    ss=job[fields.index('JOB_NAME')] + " Job scheduled at a unix timestamp of " + str(machine_state_index[start_index]) + ". It runs on "+  str(job[fields.index('NODES_REQUESTED')]) + " nodes, and finishes execution at a unix timestamp of " +  str(int(machine_state_index[start_index])+ int(job[fields.index('RUNTIME_SECONDS')]))
                    print(ss)
                    output_list.append(ss)
                    for j in range(int(job[fields.index("RUNTIME_SECONDS")])):
                        machine_state_reserved[res_ind][start_index + j] += nodes_requested
                    sched_indices.append(i)
                else:
                    break
            else:
                if nodes_requested > int(node_num)-sum(reserved_queue_nodes):
                    ss=job[fields.index('JOB_NAME')] + " cannot be scheduled due to insufficient nodes reserved in the system."
                    print(ss)
                    output_list.append(ss)
                    sched_indices.append(i)
                    break
                elif machine_state[start_index] + nodes_requested <= int(node_num)-sum(reserved_queue_nodes):
                    ss=job[fields.index('JOB_NAME')] + " Job scheduled at a unix timestamp of " + str(machine_state_index[start_index]) + ". It runs on "+  str(job[fields.index('NODES_REQUESTED')]) + " nodes, and finishes execution at a unix timestamp of " +  str(int(machine_state_index[start_index])+ int(job[fields.index('RUNTIME_SECONDS')]))
                    print(ss)
                    output_list.append(ss)
                    for j in range(int(job[fields.index("RUNTIME_SECONDS")])):
                        machine_state[start_index + j] += nodes_requested
                    sched_indices.append(i)
                elif is_backfill==1:
                    back=i+1
                    while back < len(sorted_jobs_deque):
                        job = sorted_jobs_deque[back]
                        if ("backfill" in job[fields.index('QUEUE_NAME')]) and (machine_state[start_index]+int(job[fields.index("NODES_REQUESTED")]) <= int(node_num)):
                            for j in range(int(job[fields.index("RUNTIME_SECONDS")])):
                                machine_state[start_index + j] += int(job[fields.index("NODES_REQUESTED")])
                            sched_indices.append(back)
                            ss=job[fields.index('JOB_NAME')] + ", a backfill job scheduled at a unix timestamp of " + str(machine_state_index[start_index]) +  ". It runs on "+  str(job[fields.index('NODES_REQUESTED')]) + " nodes, and finishes execution at a unix timestamp of " +  str(int(machine_state_index[start_index])+ int(job[fields.index('RUNTIME_SECONDS')]))
                            print(ss)
                            output_list.append(ss)
                            break
                        back+=1
                    break
                else:
                    break
        for s in sorted(sched_indices, reverse=True):
            sorted_jobs_deque.remove(sorted_jobs_deque[s])     
        start_index += 1
        sched_indices = []      
    return machine_state, machine_state_index, list(sorted_jobs_deque), machine_state_reserved


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
    machine_state_reserved=[[0 for i in range(start_timestamp, int(1.5 * end_timestamp))] for j in range(sum(1 for element in reserve_nodes if element != 0))] ##
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
            machine_state, machine_state_index, in_all_queue_list, machine_state_reserved = simulate_execution(in_all_queue_list, user_selected_attributes, user_selected_weights, fields, machine_state, machine_state_index, current_timestamp, machine_state_reserved)
        current_timestamp += scheduling_interval
        if len(job_fields) == 0:
            done = True
    for job in in_all_queue_list:
        ss=str(job[0]) + " could not start execution within the set start and end timeframes."
        print(ss)
        output_list.append(ss)   

def write_output():
    with open('output.log', 'w') as f:
        for item in output_list:
            f.write(item + '\n')
            
if __name__ == "__main__":
    # machine_name = "polaris" ##
    # start_timeframe = "2023-02-10 19:00:00" ##
    # end_timeframe = "2023-02-14 19:00:00" ##
    #is_backfill=1 ##
    #set_priorities = "analysis:2, demand:4"##
    #set_reservations = "preemptable:56, analysis:10" ##
    #user_selected_attributes = ["QUEUED_TIMESTAMP", "WALLTIME_SECONDS"] ##
    #user_selected_weights = [2,1] ##
    parser = argparse.ArgumentParser(description='Scheduling simulator')
    parser.add_argument('--machine_name', type=str, help='System name')
    parser.add_argument('--start_time', type=str, help='Start time of simulation')
    parser.add_argument('--end_time', type=str, help='End time of simulation')
    parser.add_argument('--backfill', action='store_true', help='Backfill set to TRUE')
    parser.add_argument('--set_priority', type=str, default="0", help='Set priorities to different queues')
    parser.add_argument('--set_reservation', type=str, default="0", help='Set reservations for different queues')
    parser.add_argument('--select_sort_formula', type=str, default="0", help='Set reservations for different queues')
    args = parser.parse_args()
    if args.machine_name is not None and args.start_time is not None and args.end_time is not None:
        machine_name=args.machine_name
        start_timeframe=args.start_time
        end_timeframe=args.end_time
    else:
        print("Not enough information available to begin simulation.")
        sys.exit()
    if args.backfill:
        is_backfill=1
    else:
        is_backfill=0
    if args.set_priority=="0":
        set_priorities=0
    else:
        set_priorities=args.set_priority
    if args.set_reservation=="0":
        set_reservations=0
    else:
        set_reservations=args.set_reservation 
    if args.select_sort_formula=="0":
        user_selected_attributes= ["QUEUED_TIMESTAMP", "WALLTIME_SECONDS"]
        user_selected_weights = [1,1]
    else:
        pairs = args.select_sort_formula.split(',')
        user_selected_attributes = []
        user_selected_weights = []
        for pair in pairs:
            parts = pair.split(':')
            attribute = parts[0].strip()
            weight = int(parts[1].strip())
            user_selected_attributes.append(attribute)
            user_selected_weights.append(weight)
    scheduling_interval = 300 ##
    output_list=[]
    fields, job_fields = request_check_and_get_data()
    node_num, all_queues, all_queues_num = sys_information(machine_name, fields, job_fields)
    queue_priorities, reserve_nodes = print_info_and_set_priorities(node_num, all_queues, all_queues_num)
    reserved_queue_name=[all_queues[i] for i in range(len(all_queues)) if reserve_nodes[i]!=0]
    reserved_queue_nodes=[reserve_nodes[i] for i in range(len(reserve_nodes)) if reserve_nodes[i]!=0]
    simulate_enqueue(node_num, fields, job_fields, user_selected_attributes, user_selected_weights)
    write_output()
    sys.exit()