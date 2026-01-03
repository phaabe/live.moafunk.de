#!/usr/bin/env bash
set -euo pipefail

# Provision (or re-provision) a Lightsail instance for the UNHEARD backend.
#
# This script WILL delete the instance if it already exists.
# Use `--yes` to confirm deletion.
#
# Requirements:
# - aws CLI configured (`aws configure`)
# - REGION set (or aws default region configured)
# - KEYPAIR already exists in Lightsail (the keypair NAME, not the .pem path)
#
# Example:
#   REGION=eu-central-1 AZ=eu-central-1a \
#   INSTANCE=unheard-backend STATIC_IP=unheard-backend-ip \
#   KEYPAIR=unheard-key2 BLUEPRINT=ubuntu_22_04 BUNDLE=small_3_0 \
#   ./backend/scripts/provision_lightsail.sh --yes

if [[ "${1:-}" != "--yes" ]]; then
  echo "Refusing to run without confirmation."
  echo "This will delete and recreate the Lightsail instance."
  echo "Re-run with: $0 --yes"
  exit 2
fi

REGION="${REGION:-$(aws configure get region 2>/dev/null || true)}"
AZ="${AZ:-}"
INSTANCE="${INSTANCE:-unheard-backend}"
STATIC_IP="${STATIC_IP:-unheard-backend-ip}"
KEYPAIR="${KEYPAIR:-}"
BLUEPRINT="${BLUEPRINT:-ubuntu_22_04}"
BUNDLE="${BUNDLE:-small_3_0}"

if [[ -z "$REGION" ]]; then
  echo "REGION is not set and no default region found."
  echo "Set REGION, e.g.: REGION=eu-central-1"
  exit 1
fi

if [[ -z "$AZ" ]]; then
  echo "AZ is not set. Set AZ, e.g.: AZ=eu-central-1a"
  exit 1
fi

if [[ -z "$KEYPAIR" ]]; then
  echo "KEYPAIR is not set. Set KEYPAIR to the Lightsail key pair name (not a .pem path)."
  exit 1
fi

aws() {
  command aws --region "$REGION" "$@"
}

instance_exists() {
  aws lightsail get-instance --instance-name "$INSTANCE" >/dev/null 2>&1
}

static_ip_exists() {
  aws lightsail get-static-ip --static-ip-name "$STATIC_IP" >/dev/null 2>&1
}

static_ip_attached_to_instance() {
  local attached
  attached=$(aws lightsail get-static-ip --static-ip-name "$STATIC_IP" --query 'staticIp.attachedTo' --output text 2>/dev/null || true)
  [[ "$attached" == "$INSTANCE" ]]
}

wait_instance_deleted() {
  echo "Waiting for instance deletion: $INSTANCE"
  for _ in {1..60}; do
    if ! instance_exists; then
      echo "Instance deleted."
      return 0
    fi
    sleep 5
  done
  echo "Timed out waiting for instance deletion."
  exit 1
}

wait_instance_running() {
  echo "Waiting for instance running: $INSTANCE"
  for _ in {1..60}; do
    local state
    state=$(aws lightsail get-instance --instance-name "$INSTANCE" --query 'instance.state.name' --output text 2>/dev/null || true)
    if [[ "$state" == "running" ]]; then
      echo "Instance is running."
      return 0
    fi
    sleep 5
  done
  echo "Timed out waiting for instance to reach running state."
  exit 1
}

echo "Region:   $REGION"
echo "AZ:       $AZ"
echo "Instance: $INSTANCE"
echo "StaticIP: $STATIC_IP"
echo "Keypair:  $KEYPAIR"
echo "OS:       $BLUEPRINT"
echo "Bundle:   $BUNDLE"

echo "\nChecking AWS identity..."
aws sts get-caller-identity >/dev/null

echo "\nIf STATIC_IP bundle error happens: ensure BUNDLE is NOT an *_ipv6_* bundle." 

if static_ip_exists && static_ip_attached_to_instance; then
  echo "Detaching static IP $STATIC_IP from $INSTANCE..."
  aws lightsail detach-static-ip --static-ip-name "$STATIC_IP" || true
fi

if instance_exists; then
  echo "Deleting existing instance $INSTANCE..."
  aws lightsail delete-instance --instance-name "$INSTANCE"
  wait_instance_deleted
else
  echo "No existing instance named $INSTANCE."
fi

# Ensure static IP exists (keep it if already allocated)
if static_ip_exists; then
  echo "Static IP $STATIC_IP already exists; reusing it."
else
  echo "Allocating static IP $STATIC_IP..."
  aws lightsail allocate-static-ip --static-ip-name "$STATIC_IP"
fi

echo "Creating instance $INSTANCE..."
aws lightsail create-instances \
  --instance-names "$INSTANCE" \
  --availability-zone "$AZ" \
  --blueprint-id "$BLUEPRINT" \
  --bundle-id "$BUNDLE" \
  --key-pair-name "$KEYPAIR" >/dev/null

wait_instance_running

echo "Attaching static IP $STATIC_IP to $INSTANCE..."
aws lightsail attach-static-ip --static-ip-name "$STATIC_IP" --instance-name "$INSTANCE"

echo "Opening firewall ports 22/80/443..."
aws lightsail open-instance-public-ports --instance-name "$INSTANCE" --port-info fromPort=22,toPort=22,protocol=tcp >/dev/null
aws lightsail open-instance-public-ports --instance-name "$INSTANCE" --port-info fromPort=80,toPort=80,protocol=tcp >/dev/null
aws lightsail open-instance-public-ports --instance-name "$INSTANCE" --port-info fromPort=443,toPort=443,protocol=tcp >/dev/null

IP=$(aws lightsail get-static-ip --static-ip-name "$STATIC_IP" --query 'staticIp.ipAddress' --output text)

echo "\nDone."
echo "Static IP: $IP"
echo "SSH: ssh -i <your.pem> ubuntu@$IP"
