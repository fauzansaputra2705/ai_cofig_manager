from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="provider_index"),
    path("settings/", views.settings_view, name="settings"),
    path("settings/save/", views.settings_save, name="settings_save"),
    path("settings/reset/<str:key>/", views.settings_reset, name="settings_reset"),
    path("settings/oauth/save/", views.oauth_save, name="oauth_save"),
    path("settings/oauth/reset/<str:key>/", views.oauth_reset, name="oauth_reset"),
    path("extensions/", views.extensions_view, name="extensions"),
    path("p/<str:key>/", views.detail, name="provider_detail"),
    path("p/<str:key>/validate/", views.validate, name="provider_validate"),
    path("p/<str:key>/diff/", views.diff_preview, name="provider_diff"),
    path("p/<str:key>/structured/diff/", views.structured_diff, name="provider_structured_diff"),
    path("p/<str:key>/signature/", views.file_signature, name="provider_signature"),
    path("p/<str:key>/test-connection/", views.connection_test, name="provider_test_connection"),
    path("p/<str:key>/download/", views.download, name="provider_download"),
    path("p/<str:key>/reload/", views.reload_from_disk, name="provider_reload"),
    path("p/<str:key>/template/", views.generate_template, name="provider_template"),
    path(
        "p/<str:key>/structured/save/",
        views.save_structured,
        name="provider_structured_save",
    ),
    # Backups
    path(
        "p/<str:key>/backups/<str:filename>/restore/",
        views.restore_backup,
        name="backup_restore",
    ),
    path(
        "p/<str:key>/backups/<str:filename>/delete/",
        views.delete_backup,
        name="backup_delete",
    ),
    # Profiles
    path("p/<str:key>/profiles/save/", views.save_profile, name="profile_save"),
    path(
        "p/<str:key>/profiles/<int:pid>/apply/",
        views.apply_profile,
        name="profile_apply",
    ),
    path(
        "p/<str:key>/profiles/<int:pid>/delete/",
        views.delete_profile,
        name="profile_delete",
    ),
    # Sessions
    path("sessions/", views.sessions_view, name="sessions"),
    path("p/<str:key>/sessions/", views.provider_sessions, name="provider_sessions"),
    path(
        "sessions/<str:provider>/<str:session_id>/",
        views.session_detail,
        name="session_detail",
    ),
    # MCP
    path("p/<str:key>/mcp/", views.mcp_view, name="provider_mcp"),
    path("p/<str:key>/mcp/save/", views.mcp_save, name="provider_mcp_save"),
    # Pi multi-file save
    path("p/<str:key>/pi-save/", views.pi_save_file, name="pi_save_file"),
    # Pi per-file validate/diff
    path(
        "p/<str:key>/pi/<str:file_type>/validate/",
        views.pi_validate,
        name="pi_validate",
    ),
    path(
        "p/<str:key>/pi/<str:file_type>/diff/",
        views.pi_diff,
        name="pi_diff",
    ),
]
