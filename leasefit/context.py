from django.conf import settings


def navigation(request):
    member = (
        getattr(request.user, "membership", None)
        if request.user.is_authenticated
        else None
    )
    return {
        "member": member,
        "support_email": settings.SUPPORT_EMAIL,
        "can_parse": request.user.is_authenticated
        and (
            request.user.is_superuser
            or (member and member.active and member.role == "manager")
        ),
    }
