from getpass import getpass
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.models import User
from django.db import transaction
from leasefit.models import Agency, Membership


class Command(BaseCommand):
    help = "Создать агентство и его первого руководителя (интерактивный пароль)"

    def add_arguments(self, p):
        p.add_argument("--name", required=True)
        p.add_argument("--email", required=True)

    @transaction.atomic
    def handle(self, *args, **o):
        email = o["email"].lower()
        validate_email(email)
        if User.objects.filter(username__iexact=email).exists():
            raise CommandError(
                "Учётная запись существует. Автоматический перенос между агентствами запрещён."
            )
        u = User(username=email, email=email)
        password = getpass("Пароль руководителя: ")
        if password != getpass("Повторите пароль: "):
            raise CommandError("Пароли различаются.")
        validate_password(password, u)
        agency = Agency.objects.create(name=o["name"].strip())
        u.set_password(password)
        u.save()
        Membership.objects.create(user=u, agency=agency, role="manager")
        self.stdout.write(self.style.SUCCESS("Агентство и руководитель созданы."))
