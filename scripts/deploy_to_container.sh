#!/bin/bash
set -e
echo "Copying updated bot files into wzml-x-app-1..."
docker cp /root/WZML-X/bot wzml-x-app-1:/usr/src/app/
echo "Restarting wzml-x-app-1 container..."
docker restart wzml-x-app-1
echo "Checking container status..."
sleep 3
docker ps --filter "name=wzml-x-app-1" --format "{{.Names}} - {{.Status}}"
