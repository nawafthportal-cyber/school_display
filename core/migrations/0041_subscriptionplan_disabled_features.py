from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0040_alter_displayscreen_occasion_theme")]

    operations = [
        migrations.AddField(
            model_name="subscriptionplan",
            name="disabled_features",
            field=models.JSONField(
                blank=True,
                default=list,
                verbose_name="خصائص موقوفة في الباقة",
            ),
        ),
    ]
