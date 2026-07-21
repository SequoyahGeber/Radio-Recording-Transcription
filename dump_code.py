import os

output_file = "project_code.txt"
# We only want code files, not images or audio
allowed_extensions = ['.py', '.html', '.css', '.js', '.bat', '.command', '.txt']
# We DO NOT want to copy the massive virtual environment or audio files
exclude_dirs = ['venv', '__pycache__', 'live_audio', 'data', '.git']

print("Scanning project...")
with open(output_file, 'w', encoding='utf-8') as outfile:
    for root, dirs, files in os.walk('.'):
        # Tell the script to ignore excluded directories
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        
        for file in files:
            if any(file.endswith(ext) for ext in allowed_extensions) and file != "dump_code.py" and file != output_file:
                filepath = os.path.join(root, file)
                
                # Write a nice header for the AI to read
                outfile.write(f"\n\n{'='*50}\n")
                outfile.write(f"FILE: {filepath}\n")
                outfile.write(f"{'='*50}\n")
                
                # Write the actual file contents
                try:
                    with open(filepath, 'r', encoding='utf-8') as infile:
                        outfile.write(infile.read())
                except Exception as e:
                    outfile.write(f"Error reading file: {e}\n")

print(f"✅ Done! All your code has been combined into '{output_file}'.")
print("You can now upload that single file to the chat!")