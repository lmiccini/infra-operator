# RabbitMQ Password Rotation

## Overview

The infra-operator supports **zero-downtime password rotation** for RabbitMQ users via a user migration approach.

## How It Works (User Migration)

Password rotation uses a **blue-green user migration** strategy:

1. **New user created**: A new RabbitMQ user is created with a rotated suffix (e.g., `foo-user-r1`)
2. **New credentials generated**: Fresh password generated for the new user
3. **Permissions granted**: New user gets same permissions on the vhost
4. **Secrets updated**: Transport URL secret updated to point to new user
5. **Services migrate**: Services gradually pick up new credentials as they reload
6. **Old user remains**: Old user (`foo-user`) stays active until manually cleaned up
7. **Zero downtime**: Both users work simultaneously during migration

## Usage

To rotate a password for a RabbitMQ user:

```bash
kubectl annotate transporturl <transporturl-name> \
  rabbitmq.openstack.org/rotate-password=true
```

### Example

```bash
# Rotate password for keystone's RabbitMQ user
kubectl annotate transporturl keystone-keystone-transport-with-user \
  rabbitmq.openstack.org/rotate-password=true
```

This will:
- Create user `foo-user-r1` with new password (if first rotation)
- Or create `foo-user-r2` (if second rotation), etc.
- Keep old user active

## Monitoring

Check the operator logs:

```bash
kubectl logs -n openstack-operators deployment/infra-operator-controller-manager | \
  grep "Password rotation"
```

Expected output:
```
Password rotation: creating new user foo-user-r1 (old: foo-user)
```

Check status:
```bash
kubectl get transporturl keystone-keystone-transport-with-user -o yaml | grep -A2 status
```

You'll see:
```yaml
status:
  rabbitmqUsername: foo-user-r1
  previousRabbitmqUsername: foo-user
```

## User Lifecycle

**After rotation, you'll have:**
- ✅ `foo-user` - old user, still active
- ✅ `foo-user-r1` - new user, now used by transport URL
- Both users can connect simultaneously

**When services reload secrets:**
- Services gradually migrate from `foo-user` to `foo-user-r1`
- No connection drops or errors

**Cleanup old user (manual):**

Once you've verified all services migrated:

```bash
# Trigger cleanup of old user
kubectl annotate transporturl keystone-keystone-transport-with-user \
  rabbitmq.openstack.org/cleanup-old-user=true
```

This deletes the old user from RabbitMQ and its secret.

## Multiple Rotations

You can rotate multiple times:
- 1st rotation: `foo-user` → `foo-user-r1`
- 2nd rotation: `foo-user-r1` → `foo-user-r2`
- 3rd rotation: `foo-user-r2` → `foo-user-r3`

Each rotation stores the previous username in status for cleanup.

## Important Notes

1. **Zero Downtime**: Both old and new users work during migration
2. **Manual Cleanup**: Old users are NOT automatically deleted
3. **Idempotent**: Safe to apply annotation multiple times
4. **Dedicated Users Only**: Only works for custom users, not default cluster user

## Verification

Check both users exist:

```bash
# List RabbitMQ users
kubectl exec -it rabbitmq-server-0 -- rabbitmqctl list_users

# Should see both:
# foo-user     []
# foo-user-r1  []
```

Check secrets:

```bash
# New user secret
kubectl get secret rabbitmq-user-foo-user-r1

# Transport URL now uses new user
kubectl get secret rabbitmq-transport-url-keystone-keystone-transport-with-user -o jsonpath='{.data.transport_url}' | base64 -d
# Output: rabbit://foo-user-r1:newpassword@...
```
