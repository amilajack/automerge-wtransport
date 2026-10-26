#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
mkdir -p certs
openssl ecparam -name prime256v1 -genkey -noout -out certs/key.pem
chmod 600 certs/key.pem
openssl req -new -x509 -key certs/key.pem -out certs/cert.pem -days 10 -subj '/CN=automerge-wtransport' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1,IP:52.9.189.226'
openssl x509 -in certs/cert.pem -outform DER | openssl dgst -sha256 -binary | xxd -p -c 256 > certs/hash.hex
openssl x509 -in certs/cert.pem -pubkey -noout | openssl pkey -pubin -outform DER | openssl dgst -sha256 -binary | openssl base64 -A > certs/spki.txt
printf '\n' >> certs/spki.txt
