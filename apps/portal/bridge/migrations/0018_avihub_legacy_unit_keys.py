from django.db import migrations


def allow_native_session_inserts(apps, schema_editor):
    """Older installations also applied a since-retired migration outside this graph."""
    model = apps.get_model("bridge", "AviHubSession")
    current_columns = {field.column for field in model._meta.local_concrete_fields}
    name = model._meta.db_table
    introspection = schema_editor.connection.introspection
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM django_migrations WHERE app = %s AND name = %s",
            ["bridge", "0016_avihub_unit_keys"],
        )
        if cursor.fetchone() is None:
            return
        columns = introspection.get_table_description(cursor, name)

    quote = schema_editor.quote_name
    for column in columns:
        if column.name in current_columns or column.null_ok or column.default is not None:
            continue
        if not {"key", "keys", "credentials"}.intersection(column.name.split("_")):
            continue
        # Keep previously stored values. Native sessions omit this retired field on INSERT.
        schema_editor.execute(f"ALTER TABLE {quote(name)} ALTER COLUMN {quote(column.name)} DROP NOT NULL")


class Migration(migrations.Migration):
    dependencies = [("bridge", "0017_platform_email")]
    operations = [migrations.RunPython(allow_native_session_inserts, migrations.RunPython.noop)]
