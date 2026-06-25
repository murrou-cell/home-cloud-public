#!/usr/bin/env sh
# Bitwarden collapses newlines to spaces in stored field values.
# Split on '-----' to reconstruct valid PEM from /ssh/id_rsa → /tmp/id_rsa.
python3 -c "
parts = open('/ssh/id_rsa').read().strip().split('-----')
header = '-----' + parts[1] + '-----'
b64    = parts[2].strip().replace(' ', '')
footer = '-----' + parts[3] + '-----'
open('/tmp/id_rsa', 'w').write(header + '\n' + b64 + '\n' + footer + '\n')
"
chmod 600 /tmp/id_rsa
