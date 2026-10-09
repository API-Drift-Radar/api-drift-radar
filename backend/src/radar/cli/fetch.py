import requests
from urllib.parse import urlsplit


def fetch_json(url):
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL must be a nonempty string.")

    if any(character.isspace() for character in url):
        raise ValueError("URL must not contain whitespace.")

    try:
        parsed = urlsplit(url)

        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError

        # Reading .port checks its format and range.
        if parsed.port == 0:
            raise ValueError
    except ValueError as error:
        raise ValueError(
            "Provide a valid HTTP or HTTPS URL with a hostname and valid port."
        ) from error

    try:
        response = requests.get(
            url,
            headers={"Accept": "application/json"},
            timeout=10,
            verify=True,
            allow_redirects=False,
        )
    except requests.exceptions.Timeout as error:
        raise RuntimeError("Request timed out.") from error
    except requests.exceptions.SSLError as error:
        raise RuntimeError("HTTPS certificate verification failed.") from error
    except requests.exceptions.RequestException as error:
        raise RuntimeError(f"Network request failed: {error}") from error

    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Request failed with HTTP status {response.status_code}.")

    try:
        return response.json()
    except ValueError as error:
        raise ValueError("Response is not valid JSON.") from error
