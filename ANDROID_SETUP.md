# Android setup

Complete the Gmail secrets and `COORDS_JSON` variable in the [README](README.md) first. Use your **private copy** throughout these instructions.

## 1. Create a restricted GitHub token

In GitHub account **Settings → Developer settings → Personal access tokens → Fine-grained tokens**, generate a token with:

- Resource owner: your account.
- Repository access: **Only select repositories** → your private `weather-alert` copy.
- Repository permissions: **Variables — Read and write**. Leave other optional permissions unset; Metadata read access is automatic.
- An expiration date you will remember to renew.

Keep the token in the phone's HTTP request block. It can change your location variable; never commit it or share an exported flow containing it.

## 2. Build the Automate flow

Install [Automate](https://llamalab.com/automate/) by LlamaLab. Create:

**Flow beginning → Time await → Location get → HTTP request → Time await**

Configure **Time await**:

- Time of day: `18:15` in the current time zone.
- Proceed: Exact.
- Wake up: enabled.

Configure **Location get**:

- Proceed: Maybe immediately.
- Provider: Balanced or High accuracy.
- Maximum fix age: 5 minutes.
- Latitude output variable: `latitude`.
- Longitude output variable: `longitude`.

Configure **HTTP request**:

- URL: `https://api.github.com/repos/YOUR_USERNAME/YOUR_REPOSITORY/actions/variables/COORDS_JSON` — replace both placeholders with your private copy's owner and name.
- Method: `PATCH`.
- Content type: `application/json`.
- Request headers (dictionary/expression mode; replace the token placeholder):

  ```text
  {
    "Accept": "application/vnd.github+json",
    "Authorization": "Bearer YOUR_FINE_GRAINED_TOKEN",
    "X-GitHub-Api-Version": "2026-03-10"
  }
  ```

- Request content body (expression mode):

  ```text
  jsonEncode({
    "name": "COORDS_JSON",
    "value": jsonEncode({
      "latitude": round(latitude * 1000) / 1000,
      "longitude": round(longitude * 1000) / 1000,
      "updated_at": Now
    })
  })
  ```

- Response status code output variable: `status_code`.

Allow background location access and exclude Automate from Android battery optimization. Location is rounded to three decimal places, but it can still identify your neighborhood.

## 3. Test and leave it running

Temporarily set **Time await** a few minutes ahead, start the flow, and inspect the HTTP response: **204** means the update succeeded. Confirm `COORDS_JSON` changed in your private repository's Variables tab. Restore `18:15` and restart the flow.

If it fails, check the private repository URL, that `COORDS_JSON` already exists, token expiration and Variables permission, network access, location permission, and battery restrictions. An HTTP connection failure may stop this simple flow; restart it after fixing the cause. The weather workflow keeps using the last location until the phone reports a newer one.

After this succeeds, return to the README and send a test email.
