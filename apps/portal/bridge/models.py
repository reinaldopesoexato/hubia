from uuid import uuid4
from django.conf import settings
from django.db import models


class Link(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    tenant = models.UUIDField()
    space = models.UUIDField()
    name = models.CharField(max_length=100)
    protocol = models.CharField(max_length=8)
    client_id = models.CharField(max_length=128, db_index=True)
    key_digest = models.CharField(max_length=64, blank=True)
    peer_config = models.BinaryField(null=True)
    writable = models.BooleanField(default=False)
    email_only = models.BooleanField(default=False)
    offer_mail_access = models.BooleanField(default=True)


    class Meta:
        constraints = [models.UniqueConstraint(fields=["client_id","user"], name="bridge_user_client")]


class Ticket(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    link = models.ForeignKey(Link, on_delete=models.CASCADE)
    request_id = models.CharField(max_length=128)
    remote_id = models.CharField(max_length=256,blank=True)
    notice_agent = models.UUIDField(null=True)
    notice_due = models.DateTimeField(null=True)
    notice_attempts = models.PositiveSmallIntegerField(default=0)
    question = models.TextField()
    answer = models.TextField(default="")
    state = models.CharField(max_length=12, default="pending")
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["link","request_id"], name="bridge_request_once")]


class Subscription(models.Model):
    id = models.CharField(max_length=64, primary_key=True)
    link = models.ForeignKey(Link, on_delete=models.CASCADE)
    encrypted = models.BinaryField()
    expires = models.DateTimeField()


class Delivery(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE)
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE)
    attempts = models.PositiveSmallIntegerField(default=0)
    due = models.DateTimeField()
    state = models.CharField(max_length=12, default="pending")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["ticket","subscription"], name="bridge_delivery_once")]


class NotificationChannel(models.Model):
    agent = models.UUIDField()
    tenant = models.UUIDField()
    space = models.UUIDField()
    channel = models.CharField(max_length=24)
    enabled = models.BooleanField(default=False)
    encrypted = models.BinaryField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "space", "agent", "channel"], name="notification_channel_once")]


class NotificationDestination(models.Model):
    channel = models.ForeignKey(NotificationChannel, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=128)
    sector = models.UUIDField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["channel", "name"], name="notification_destination_name")]


class GitHubConnection(models.Model):
    tenant = models.UUIDField(primary_key=True)
    organization = models.CharField(max_length=39)
    app_id = models.BigIntegerField(unique=True)
    slug = models.CharField(max_length=100)
    private_key = models.BinaryField()
    installation_id = models.BigIntegerField(null=True, unique=True)
    repositories = models.JSONField(default=list)
    checked_at = models.DateTimeField(null=True)
    error = models.TextField(blank=True)


class NoteLibrary(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    connection = models.OneToOneField(GitHubConnection, on_delete=models.CASCADE)
    name = models.CharField(max_length=120)
    repository_id = models.BigIntegerField()


class LibraryAccess(models.Model):
    library = models.ForeignKey(NoteLibrary, on_delete=models.CASCADE)
    space = models.UUIDField()
    folders = models.JSONField(default=list)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["library", "space"], name="library_space_once")]


class CatalogAlerts(models.Model):
    tenant = models.UUIDField()
    space = models.UUIDField()
    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "space"], name="catalog_alerts_space")]


class MailProvider(models.Model):
    tenant = models.UUIDField(primary_key=True)
    domain = models.CharField(max_length=253)
    enabled = models.BooleanField(default=True)


class Mailbox(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    provider = models.ForeignKey(MailProvider, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    address = models.EmailField()
    password_cipher = models.BinaryField()
    signature_html = models.TextField(blank=True, default="")
    links = models.ManyToManyField(Link, blank=True)
    send_links = models.ManyToManyField(Link, blank=True, related_name='outbound_mailboxes')

    class Meta:
        constraints = [models.UniqueConstraint(fields=["provider", "user"], name="mailbox_per_member")]


class WorkspaceSelection(models.Model):
    link = models.OneToOneField(Link, on_delete=models.CASCADE, primary_key=True)
    # Null includes future workspaces. An empty list pauses access.
    spaces = models.JSONField(null=True)


class OutboundEmail(models.Model):
    link = models.ForeignKey(Link, on_delete=models.CASCADE)
    request_id = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    state = models.CharField(max_length=16, default="registrado")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["link", "request_id"], name="mail_send_per_link")]


class CatalogObservation(models.Model):
    token = models.OneToOneField("oauth2_provider.AccessToken", on_delete=models.CASCADE, primary_key=True)
    link = models.ForeignKey(Link, on_delete=models.CASCADE)
    revision = models.CharField(max_length=64, blank=True)
    listed_at = models.DateTimeField(null=True)
    announced = models.CharField(max_length=64, blank=True)
    announced_at = models.DateTimeField(null=True)
