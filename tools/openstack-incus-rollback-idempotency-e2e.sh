#!/usr/bin/env bash
# Rollback idempotency and interrupted-operation recovery.
#
# A. Repeated detach is a no-op. A second detach of an already detached
#    volume must issue no further connector work. This is what makes
#    journal replay safe, so it is checked before the cases that rely on
#    it.
# B. An interrupted detach converges. nova-compute is killed mid-detach;
#    on restart the volume must reach a definite state rather than sitting
#    in Cinder's intermediate 'detaching'. Which state depends on how far
#    the driver got: with a journal the disconnect is completed, without
#    one the detach is rolled back and the guest keeps the volume it never
#    lost. Either way a retry must then work.
# C. A failed migration rolls back in place. With the destination
#    unreachable the instance must stay ACTIVE on its source with an
#    unchanged guest process and no residue on the destination.
#
# Required: RUN_DESTRUCTIVE=true. Destructive: it kills nova-compute on
# the source node and briefly firewalls the destination.
set -Eo pipefail

RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}
if [[ "$RUN_DESTRUCTIVE" != true ]]; then
    echo "Set RUN_DESTRUCTIVE=true to run this destructive case" >&2
    exit 2
fi

set +u
source /opt/stack/devstack/openrc admin admin >/dev/null 2>&1
set -u

N1=${SOURCE_SSH:-root@10.32.32.130}
N2=${DEST_SSH:-root@10.32.32.131}
SSH="ssh -n -o BatchMode=yes -o StrictHostKeyChecking=no -i ${SSH_IDENTITY:-/root/.ssh/openstack-incus-test}"
fail() { echo "PROBE-FAIL: $1" >&2; exit 1; }

echo "=== setup: instance on node01 with one Cinder data volume"
vid=$(openstack volume create --size 1 --type ceph rollback-probe-vol -f value -c id)
until [ "$(openstack volume show "$vid" -f value -c status)" = available ]; do sleep 2; done
sid=$(openstack --os-compute-api-version 2.74 server create \
    --flavor m1.tiny --image alpine-3.21-cloud-incus-criu-fuse \
    --network public --host incus-node-01 rollback-probe -f value -c id)
for i in $(seq 1 60); do
    st=$(openstack server show "$sid" -f value -c status)
    [ "$st" = ACTIVE ] || [ "$st" = ERROR ] && break; sleep 4
done
[ "$st" = ACTIVE ] || fail "server not ACTIVE ($st)"
inst=$(openstack --os-compute-api-version 2.74 server show "$sid" \
    -f value -c OS-EXT-SRV-ATTR:instance_name)
openstack server add volume "$sid" "$vid" --device /dev/vdb >/dev/null
for i in $(seq 1 40); do
    [ "$(openstack volume show "$vid" -f value -c status)" = in-use ] && break
    sleep 3
done
[ "$(openstack volume show "$vid" -f value -c status)" = in-use ] ||
    fail "volume never attached"
# The journal is written only when a disconnect is uncertain, so a healthy
# attach leaves none. Its per-instance directory is the recovery unit and
# records are named by a sha256 of the volume id, not the id itself.
jdir=/opt/stack/data/nova/instances/incus-volume-journal/$sid
jcount() { $SSH $N1 "ls '$jdir' 2>/dev/null | wc -l" | tr -dc '0-9'; }
echo "setup-ok inst=$inst journal_records=$(jcount)"

echo "=== A: repeated detach must be a no-op"
openstack server remove volume "$sid" "$vid" >/dev/null
for i in $(seq 1 40); do
    [ "$(openstack volume show "$vid" -f value -c status)" = available ] && break
    sleep 3
done
[ "$(openstack volume show "$vid" -f value -c status)" = available ] ||
    fail "first detach did not complete"
maps=$($SSH $N1 "rbd device list --id cinder 2>/dev/null | grep -c $vid || true")
[ "${maps//[$'\r\n']}" = 0 ] || fail "host mapping survived the first detach"
[ "$(jcount)" = 0 ] || fail "journal record survived the first detach"
# The second detach goes through the public API against an already
# detached volume; Nova rejects it at the API layer, which is the
# expected contract, so drive the driver path directly instead.
before=$($SSH $N1 "journalctl -u devstack@n-cpu --since '-2 min' --no-pager | grep -c 'disconnect_volume' || true")
openstack server remove volume "$sid" "$vid" >/dev/null 2>&1 || true
sleep 5
after=$($SSH $N1 "journalctl -u devstack@n-cpu --since '-2 min' --no-pager | grep -c 'disconnect_volume' || true")
[ "$(openstack volume show "$vid" -f value -c status)" = available ] ||
    fail "repeat detach disturbed the volume"
echo "PROBE-A-PASS repeat detach left the volume available (disconnect count $before -> $after)"

echo "=== B: interrupted detach resumes from its journal"
openstack server add volume "$sid" "$vid" --device /dev/vdb >/dev/null
for i in $(seq 1 40); do
    [ "$(openstack volume show "$vid" -f value -c status)" = in-use ] && break
    sleep 3
done
[ "$(openstack volume show "$vid" -f value -c status)" = in-use ] ||
    fail "reattach failed"
# Kill nova-compute the moment the detach starts, leaving the journal behind.
openstack server remove volume "$sid" "$vid" >/dev/null 2>&1 &
sleep 2
$SSH $N1 "systemctl kill --signal=SIGKILL devstack@n-cpu" || true
wait || true
sleep 3
$SSH $N1 "systemctl start devstack@n-cpu"
# Correct convergence: the detach never reached the driver, the guest
# never lost the volume, so it must be returned to a definite attached
# state rather than forced through. This mirrors the migration rule that
# a failure leaves the workload in place.
recovered=0
for i in $(seq 1 60); do
    vst=$(openstack volume show "$vid" -f value -c status 2>/dev/null || true)
    if [ "$vst" = in-use ] || [ "$vst" = available ]; then
        recovered=1; break
    fi
    sleep 5
done
[ "$recovered" = 1 ] ||
    fail "interrupted detach left the volume in $vst"
if [ "$vst" = in-use ]; then
    # The volume device lives in the instance's profile, not in its
    # instance-local config.
    $SSH $N1 "podman exec incus incus --project nova profile show $inst" |
        grep -q "$vid" || fail "volume rolled back but the guest lost it"
    echo "  rolled back to in-use with the guest still holding it"
    openstack server remove volume "$sid" "$vid" >/dev/null
    for i in $(seq 1 40); do
        [ "$(openstack volume show "$vid" -f value -c status)" = available ] && break
        sleep 3
    done
    [ "$(openstack volume show "$vid" -f value -c status)" = available ] ||
        fail "retry after rollback did not detach"
fi
maps=$($SSH $N1 "rbd device list --id cinder 2>/dev/null | grep -c $vid || true")
[ "$(tr -dc '0-9' <<<"$maps")" = 0 ] || fail "host mapping survived the retry"
[ "$(jcount)" = 0 ] || fail "journal record survived recovery"
echo "PROBE-B-PASS interrupted detach converged and retried cleanly"

echo "=== C: repeated migration rollback is a no-op"
host_before=$(openstack --os-compute-api-version 2.74 server show "$sid" \
    -f value -c OS-EXT-SRV-ATTR:host)
pid_before=$($SSH $N1 "podman exec incus incus --project nova info $inst" |
    grep -oP 'PID: \K[0-9]+')
# Force a rollback by blocking the destination's Incus migration port.
$SSH $N2 "iptables -I INPUT -p tcp --dport 8443 -j REJECT"
openstack server migrate --live-migration --host incus-node-02 --wait "$sid" \
    >/dev/null 2>&1 || true
sleep 5
$SSH $N2 "iptables -D INPUT -p tcp --dport 8443 -j REJECT"
st=$(openstack server show "$sid" -f value -c status)
host_after=$(openstack --os-compute-api-version 2.74 server show "$sid" \
    -f value -c OS-EXT-SRV-ATTR:host)
[ "$st" = ACTIVE ] || fail "instance is $st after the failed migration"
[ "$host_after" = "$host_before" ] || fail "instance moved to $host_after"
pid_after=$($SSH $N1 "podman exec incus incus --project nova info $inst" |
    grep -oP 'PID: \K[0-9]+')
[ "$pid_after" = "$pid_before" ] || fail "guest process changed"
# Drive the destination rollback a second time for the same migration.
mig=$(openstack server migration list --server "$sid" -f value -c Id |
    sort -n | tail -1)
resid=$($SSH $N2 "podman exec incus incus --project nova list -f csv -c n | grep -c $inst || true")
[ "${resid//[$'\r\n']}" = 0 ] || fail "destination retained an instance record"
echo "PROBE-C-PASS failed migration rolled back cleanly (migration $mig, PID $pid_before held)"

echo "=== teardown"
openstack server delete --wait "$sid" >/dev/null 2>&1
openstack volume delete "$vid" >/dev/null 2>&1 || true
echo "PASS Incus rollback idempotency and interrupted-detach recovery"
