"""Build the driver and run it on a store with nothing to recover."""
import subprocess

subprocess.run(['make', '-C', '/app'], check=True)
text = '100 1 0 0\n100 10 20 30 40 50 60 70 80\n'
result = subprocess.run(['/app/recovery'], input=text, text=True, capture_output=True, timeout=10)
assert result.returncode == 0, result.stderr
for line in result.stdout.splitlines():
    fields = line.split()
    assert fields[0] == 'P' and list(map(int, fields[1:])) == [0, 100, 10, 20, 30, 40, 50, 60, 70, 80], line
print('Smoke check passed. It does not test recovery.')
