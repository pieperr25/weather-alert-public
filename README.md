# Daily weather email

Get tomorrow's rain, wind, and severe-weather forecast by email, sent from your Gmail address to that same address. Your Android phone supplies the location. Each email shows the town/city name, or county when available.

## Install

You need a GitHub account, an Android phone with [Automate](https://llamalab.com/automate/), and a Gmail account that supports app passwords. No server or Python installation is needed.

### 1. Make your private copy

On this repository, choose **Use this template → Create a new repository**. Choose your own account, name it `weather-alert`, select **Private**, and create it. Use the template button rather than Fork so you can choose private visibility.

All remaining steps happen in **your private copy**. The email workflow only runs in private repositories. If Actions is disabled, enable it under the repository's **Actions** tab.

### 2. Connect your Gmail

Enable Google [2-Step Verification and create an app password](https://support.google.com/accounts/answer/185833) for this app. Use the app password, not your regular Gmail password. If Google does not offer app passwords for your account, you need an eligible account.

In your private repository, open **Settings → Secrets and variables → Actions → Secrets**. Add these repository secrets:

| Name | Value |
| --- | --- |
| `SMTP_USERNAME` | Your Gmail address |
| `SMTP_PASSWORD` | Your Google app password (omit spaces) |
| `SMTP_FROM` | The same Gmail address |
| `SMTP_TO` | The same Gmail address |
| `NWS_USER_AGENT` | `daily-weather-alert/1.0 (you@example.com)`, replacing the example with your email |

### 3. Set your starting location

In **Settings → Secrets and variables → Actions → Variables**, add a repository variable named `COORDS_JSON`:

```json
{"latitude": 40.0, "longitude": -100.0, "updated_at": "2026-01-01T00:00:00Z"}
```

Replace the sample coordinates with your location and the timestamp with the current UTC time. The phone will overwrite this value; the workflow keeps using the last reported location if an update is missed.

### 4. Connect your phone

Follow [Android setup](ANDROID_SETUP.md) to send your location to **your private repository** every day at 18:15. Run the phone flow once and confirm HTTP status `204` before continuing.

### 5. Test your email

In your private repository, open **Actions → Daily next-day weather alert → Run workflow**. Check **Send a test email without evaluating the forecast**, then run it. Check your inbox and spam folder. This test sends an email regardless of the weather or time of day.

Leave the phone flow running. GitHub checks hourly and evaluates tomorrow's forecast on the first run at or after 18:00 in the location's time zone; scheduled runs may be delayed. An email is sent when the rain, sustained-wind, or severe-outlook rules trigger. Workflow failures attempt to send a separate error email using the same Gmail account. If Gmail or its credentials fail, check the Actions page instead.

## What to expect

Tomorrow's 06:00–19:00 forecast triggers for any hour with at least 50% rain probability, two consecutive hours with at least 30%, or sustained wind of at least 15 mph. Official next-day severe outlooks cover the United States and metropolitan France. Regular rain/wind forecasts also work elsewhere. These are next-day forecasts, not live emergency warnings.

Your copy, settings, and workflow logs stay in your private GitHub repository, accessible to you and anyone you grant access. GitHub runs the checks; this does not run entirely on your phone. Forecast and location services receive location queries, and Gmail handles the emails. No data is sent to this template's maintainer. Keep your copy private and keep tokens, location files, and credentials out of commits and shared phone-flow exports.

To pause emails, disable **Daily next-day weather alert** from its Actions menu. To uninstall, stop the phone flow, revoke its GitHub token and Google app password, and delete your private repository if desired.

For development: Python 3.10+ with no third-party dependencies; run `python3 -m unittest -v test_weather_alert.py`.
