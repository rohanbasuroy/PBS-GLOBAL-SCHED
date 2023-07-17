import argparse

# Create an ArgumentParser object
parser = argparse.ArgumentParser(description='My Python Program')

# Add flags
parser.add_argument('--flag1', action='store_true', help='Description of flag 1')
parser.add_argument('--flag2', type=str, help='Description of flag 2')

# Parse the command-line arguments
args = parser.parse_args()

# Access the flag values
if args.flag1:
    print("Flag 1 is set.")

if args.flag2 is not None:
    print("Flag 2 is set to:"+ args.flag2)
