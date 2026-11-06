# School occasion themes

Managers can open **قوالب المناسبات → تعديل اسم الثيم وتصميمه** or the customization
link next to **ثيم المناسبة** in an announcement form. Each school can customize
the theme name, tagline, background/primary/highlight/text colors, and background
pattern for each built-in occasion. The customization link opens separately so
an unsaved announcement draft stays open.

Customization changes the presentation for that school's screens using the
occasion; announcement targeting, timing and activation still control where and
when it appears. Saving an appearance does not activate an occasion. The original
occasion identity and calendar remain in `core/occasions.py`.

`SchoolOccasionTheme` has a unique school/occasion key. Colors are HEX-only,
patterns are selected from a fixed list, text contrast is checked on the server,
and user strings use escaped HTML/JSON and `textContent`. Resetting removes only
that school's override. Save and reset invalidate snapshots and refresh screens.

Migration: `schedule.0026_schooloccasiontheme` is additive. Rebuild all Django
services, apply migrations before restarting, and verify no migrations remain.
Display snapshots include `settings.occasion_customizations`; live rendering
also retains the HTML bootstrap values when a legacy snapshot omits that field.
The occasion appearance resets when switching to another occasion or restoring
defaults, while emergency presentation retains its own colors.
