import csv
import os
import sys


def add_job(job_id, requested_nodes, requested_walltime, queued_time, file_path):
    new_job = {
        'Job ID': job_id,
        'Requested Nodes': requested_nodes,
        'Requested Walltime': requested_walltime,
        'Queued Time': queued_time
    }

    with open(file_path, 'a', newline='') as csvfile:
        fieldnames = ['Job ID', 'Requested Nodes', 'Requested Walltime', 'Queued Time']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        writer.writerow(new_job)
        print(f"Job '{job_id}' added successfully.")


def delete_job(job_id, file_path):
    temp_file = file_path + '.tmp'

    with open(file_path, 'r') as csvfile, open(temp_file, 'w', newline='') as temp_csvfile:
        reader = csv.DictReader(csvfile)
        fieldnames = reader.fieldnames

        writer = csv.DictWriter(temp_csvfile, fieldnames=fieldnames)
        writer.writeheader()

        deleted = False

        for row in reader:
            if row['Job ID'] == job_id:
                deleted = True
                continue
            writer.writerow(row)

    if deleted:
        os.replace(temp_file, file_path)
        print(f"Job '{job_id}' deleted successfully.")
    else:
        print(f"Job '{job_id}' not found.")


if __name__ == '__main__':
    path_to_server_logs = sys.argv[1]
    file_path = "./"+path_to_server_logs+'/server_logs.csv'

    if len(sys.argv) < 2:
        print("Invalid command. Please use one of the following options:")
        print("python3 simsh.py qadd <job_id> <requested_nodes> <requested_walltime> <queued_time>")
        print("python3 simsh.py qdel <job_id>")
        sys.exit(1)

    command = sys.argv[2]

    if command == 'qadd':
        if len(sys.argv) != 7:
            print("Invalid command. Please use the following format:")
            print("python3 simsh.py qadd <job_id> <requested_nodes> <requested_walltime> <queued_time>")
            sys.exit(1)

        job_id = sys.argv[3]
        requested_nodes = int(sys.argv[4])
        requested_walltime = float(sys.argv[5])
        queued_time = float(sys.argv[6])

        add_job(job_id, requested_nodes, requested_walltime, queued_time, file_path)

    elif command == 'qdel':
        if len(sys.argv) != 4:
            print("Invalid command. Please use the following format:")
            print("python3 simsh.py qdel <job_id>")
            sys.exit(1)

        job_id = sys.argv[3]
        delete_job(job_id, file_path)

    else:
        sys.exit(1)
