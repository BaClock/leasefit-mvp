from django.contrib import admin
from django.contrib.auth import views as auth
from django.urls import path
from django.shortcuts import redirect
from leasefit import views as v

urlpatterns = [
    path("healthz", v.healthz, name="healthz"),
    path("", lambda r: redirect("requests" if r.user.is_authenticated else "login")),
    path("admin/", admin.site.urls),
    path(
        "login",
        auth.LoginView.as_view(template_name="registration/login.html"),
        name="login",
    ),
    path("logout", auth.LogoutView.as_view(), name="logout"),
    path("invite/<uuid:token>", v.activate, name="activate"),
    path(
        "password-reset",
        auth.PasswordResetView.as_view(
            template_name="registration/reset.html",
            email_template_name="registration/reset_email.txt",
            subject_template_name="registration/reset_subject.txt",
            success_url="/password-reset/sent",
        ),
        name="password_reset",
    ),
    path(
        "password-reset/sent",
        auth.PasswordResetDoneView.as_view(
            template_name="registration/reset_done.html"
        ),
        name="password_reset_done",
    ),
    path(
        "password-reset/<uidb64>/<token>",
        auth.PasswordResetConfirmView.as_view(
            template_name="registration/reset_confirm.html", success_url="/login"
        ),
        name="password_reset_confirm",
    ),
    path("pilot", v.pilot, name="pilot"),
    path("privacy", v.privacy, name="privacy"),
    path("example", v.example, name="example"),
    path("app/premises", v.all_premises, name="all_premises"),
    path("app/premises/new", v.start_premises, name="start_premises"),
    path("app/premises/<int:object_id>", v.premises_edit, name="catalog_premises"),
    path(
        "app/premises/<int:object_id>/delete",
        v.delete_premises,
        name="delete_premises",
    ),
    path(
        "app/premises/<int:object_id>/observations",
        v.add_observation,
        name="catalog_observation",
    ),
    path(
        "app/premises/<int:object_id>/observations/<int:observation_id>/resolve",
        v.resolve,
        name="catalog_resolve",
    ),
    path("app/premises/<int:object_id>/upload", v.upload, name="catalog_upload"),
    path(
        "app/premises/<int:object_id>/characteristics",
        v.custom_characteristic,
        name="custom_characteristic",
    ),
    path(
        "app/requests/<int:pk>/objects/select",
        v.select_premises,
        name="select_premises",
    ),
    path(
        "app/requests/<int:pk>/objects/<int:object_id>/detach",
        v.detach_premises,
        name="detach_premises",
    ),
    path(
        "app/requests/<int:pk>/objects/<int:object_id>/context",
        v.request_context,
        name="request_context",
    ),
    path(
        "app/requests/<int:pk>/template",
        v.apply_business_template,
        name="business_template",
    ),
    path("app/parser", v.parser_preview, name="parser"),
    path("app/requests/<int:pk>/refine", v.refine, name="refine"),
    path("app/requests", v.requests_list, name="requests"),
    path("app/requests/new", v.profile, name="request_new"),
    path("app/requests/<int:pk>/profile", v.profile, name="profile"),
    path("app/requests/<int:pk>/delete", v.delete_request, name="delete_request"),
    path("app/requests/<int:pk>/state", v.lifecycle, name="lifecycle"),
    path("app/requests/<int:pk>/objects", v.objects, name="objects"),
    path("app/requests/<int:pk>/objects/new", v.premises_edit, name="premises_new"),
    path(
        "app/requests/<int:pk>/objects/<int:object_id>",
        v.premises_edit,
        name="premises",
    ),
    path(
        "app/requests/<int:pk>/objects/<int:object_id>/observations",
        v.add_observation,
        name="observation_add",
    ),
    path(
        "app/requests/<int:pk>/objects/<int:object_id>/observations/<int:observation_id>/resolve",
        v.resolve,
        name="resolve",
    ),
    path(
        "app/requests/<int:pk>/objects/<int:object_id>/upload", v.upload, name="upload"
    ),
    path("app/requests/<int:pk>/checks", v.checks, name="checks"),
    path("app/requests/<int:pk>/compare", v.compare, name="compare"),
    path("app/requests/<int:pk>/reports", v.reports, name="reports"),
    path(
        "app/files/<int:file_id>/download",
        v.attachment_download,
        name="attachment_download",
    ),
    path(
        "app/reports/<int:report_id>/download",
        v.report_download,
        name="report_download",
    ),
    path("app/team", v.team, name="team"),
    path("app/settings", v.settings_view, name="settings"),
]
