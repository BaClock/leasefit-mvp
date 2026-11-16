from django.contrib import admin
from .models import Agency, Membership, PilotApplication, AuditEvent

admin.site.register(Agency)


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "agency", "role", "active"]
    # Role reassignment and agency transfer need an explicit operator workflow.
    readonly_fields = ["agency", "user"]

    def has_add_permission(self, request):
        return False


@admin.register(PilotApplication)
class PilotAdmin(admin.ModelAdmin):
    list_display = ["agency", "city", "person", "status", "created_at"]
    readonly_fields = [
        "agency",
        "city",
        "person",
        "contact",
        "types",
        "comment",
        "consent",
        "creation_key",
        "fingerprint",
        "created_at",
    ]

    def has_add_permission(self, request):
        return False


@admin.register(AuditEvent)
class AuditAdmin(admin.ModelAdmin):
    list_display = ["actor", "agency", "action", "resource", "created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
