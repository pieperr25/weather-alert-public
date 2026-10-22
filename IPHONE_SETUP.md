# iPhone setup

Complete the email secrets and `COORDS_JSON` variable in the [README](README.md) first. Use **your private repository** throughout. Your iPhone sends its location to that repository; the weather workflow reads it and sends email using your email account.

You need Apple Shortcuts, Location Services, and an internet connection. There is no extra server or paid app.

## 1. Create a restricted GitHub token

In GitHub account **Settings → Developer settings → Personal access tokens → Fine-grained tokens**, generate a token with:

- Resource owner: your account.
- Repository access: **Only select repositories** → your private `weather-alert` copy.
- Repository permissions: **Variables — Read and write**. Leave other optional permissions unset; Metadata read access is automatic.
- An expiration date you will remember to renew.

The token stays in your shortcut. Do not publish it, share the configured shortcut, or include it in screenshots. If you sync Shortcuts through iCloud, the configured shortcut can also sync to your other Apple devices.

## 2. Build the location shortcut

Open **Shortcuts**, create a new shortcut, and name it **Update Weather Location**. Add these actions in order. Names and menu placement can vary by iOS version.

When a step refers to an earlier result, tap the input and use **Select Variable** to choose that action's output. Insert variable tokens, not their names as literal text.

### Capture coordinates and time

1. **Get Current Location**.
2. **Get Details of Locations**: choose **Latitude** and explicitly select **Current Location** from step 1 as the input.
3. **Round Number**: round that latitude to **Thousandths** (3 decimal places). **Set Variable** to `Latitude`.
4. **Get Details of Locations**: choose **Longitude** and explicitly select **Current Location** from step 1 again, rather than the previous number.
5. **Round Number**: round that longitude to **Thousandths**. **Set Variable** to `Longitude`.
6. **Date**: choose **Current Date**.
7. **Format Date**: use that date, choose **ISO 8601**, and enable **Include Time** if shown. **Set Variable** to `Timestamp`. The result must include a time zone, for example `2026-08-08T18:15:00-05:00` or a trailing `Z`.

### Make the location JSON

Add a **Dictionary** action with these fields:

| Key | Field type | Value |
| --- | --- | --- |
| `latitude` | Number | `Latitude` variable |
| `longitude` | Number | `Longitude` variable |
| `updated_at` | Text | `Timestamp` variable |

Add **Get Text from Input**, selecting that Dictionary as the input, then **Set Variable** to `CoordinatesJSON`. This converts the dictionary to JSON text. Using number fields avoids decimal-separator problems on phones with different regional settings.

The resulting text has this shape (your shortcut fills in the real numbers and time):

```json
{"latitude": 40.123, "longitude": -100.456, "updated_at": "2026-08-08T18:15:00-05:00"}
```

### Send it to your repository

Add a **URL** action:

```text
https://api.github.com/repos/YOUR_USERNAME/YOUR_REPOSITORY/actions/variables/COORDS_JSON
```

Replace both placeholders with your private copy's owner and name.

Add **Get Contents of URL**, selecting that URL, and expand its options:

- Method: **PATCH**.
- Headers:

  | Key | Value |
  | --- | --- |
  | `Accept` | `application/vnd.github+json` |
  | `Authorization` | `Bearer YOUR_FINE_GRAINED_TOKEN` (replace the placeholder; keep `Bearer `) |
  | `X-GitHub-Api-Version` | `2026-03-10` |
  | `Content-Type` | `application/json` |

- Request Body: **JSON**, with these fields:

  | Key | Field type | Value |
  | --- | --- | --- |
  | `name` | Text | `COORDS_JSON` (literal text) |
  | `value` | Text | `CoordinatesJSON` variable |

The `value` must be **text containing JSON**, not a nested Dictionary. GitHub stores the text as the repository variable. Do not type your location into the request manually.

## 3. Run it once

Run **Update Weather Location** while the iPhone is unlocked. Allow location and `api.github.com` network access when asked; choose ongoing permission when offered so later runs do not wait for a prompt. Ensure **Settings → Privacy & Security → Location Services** is enabled and Shortcuts has location access.

GitHub returns an empty response on success (HTTP `204`), so an empty Shortcuts result is normal. Confirm success by opening **your private repository → Settings → Secrets and variables → Actions → Variables** and checking that `COORDS_JSON` contains your coordinates and a new timestamp. Round to three decimals still reveals your approximate neighborhood; keep the repository private.

## 4. Schedule the daily update

In Shortcuts, add a **Time of Day** personal automation for **18:15 / 6:15 PM**, repeating **Daily**, and set its action to **Run Shortcut → Update Weather Location**. Choose **Run Immediately**; on versions using **Ask Before Running**, turn it off and confirm. On versions that offer an automation trigger directly in the shortcut editor, attach the same daily trigger there.

Test the automation at a time a few minutes ahead, including with the phone locked, and confirm the repository timestamp changes. Then restore **18:15**. Complete any permission prompts by running the shortcut manually first. iOS permissions, device state, and connectivity can affect unattended runs; GitHub keeps using the last reported location if an update is missed.

After the location update succeeds, return to the README and send a test email. Keep this automation enabled on one phone per repository.

## Troubleshooting

- **Variable did not change:** inspect the shortcut's error, private repository URL, and token expiration/Variables permission. Make sure `COORDS_JSON` already exists.
- **Validation error:** the request's `value` must be Text from `CoordinatesJSON`; latitude/longitude inside it must be Numbers, and the ISO timestamp must include the time zone.
- **Manual runs work, scheduled runs do not:** confirm the automation is enabled and runs immediately, grant location/network permissions manually, and retest with the phone locked.
- **Emails use an old location:** check the variable's timestamp; run the shortcut again and fix the phone automation. Updates after the day's completed weather check take effect on the next check.

To stop location updates, disable the automation and revoke its GitHub token. To stop emails too, disable the weather workflow in your private repository.

Apple references: [API requests in Shortcuts](https://support.apple.com/guide/shortcuts/request-your-first-api-apd58d46713f/ios), [ISO 8601 dates](https://support.apple.com/guide/shortcuts/technical-standards-apdfd459e13d/ios), and [automatic personal automations](https://support.apple.com/guide/shortcuts/enable-or-disable-a-personal-automation-apd602971e63/ios).
