import json
from decimal import Decimal
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from app.providers.omdb import (
    OmdbAuthError,
    OmdbBudgetError,
    OmdbRequestError,
    fetch_ratings,
)

OMDB_HIT = {
    "Response": "True",
    "imdbRating": "8.7",
    "imdbVotes": "1,234,567",
    "Metascore": "73",
    "Ratings": [
        {"Source": "Internet Movie Database", "Value": "8.7/10"},
        {"Source": "Rotten Tomatoes", "Value": "83%"},
        {"Source": "Metacritic", "Value": "73/100"},
    ],
}


class OmdbClientTests(SimpleTestCase):
    """Tests for the OMDb client."""

    def setUp(self):
        """Drop cached OMDb responses between tests."""
        cache.clear()

    @override_settings(OMDB_API_KEY="")
    @patch("app.providers.omdb.services.api_request")
    def test_no_api_key_skips_the_request(self, mock_request):
        """A missing key leaves the ratings empty and does not call OMDb."""
        self.assertIsNone(fetch_ratings("tt0133093"))
        mock_request.assert_not_called()

    @override_settings(OMDB_API_KEY="test-key", OMDB_DAILY_LIMIT=900)
    @patch("app.providers.omdb.services.api_request", return_value=OMDB_HIT)
    def test_parses_mocked_response(self, mock_request):
        """IMDb, critics, and Metacritic are read from a mocked OMDb body."""
        parsed = fetch_ratings("tt0133093")

        self.assertEqual(parsed["imdb"], Decimal("8.7"))
        self.assertEqual(parsed["imdb_votes"], 1234567)
        self.assertEqual(parsed["rotten_tomatoes"], Decimal(83))
        self.assertEqual(parsed["metacritic"], Decimal(73))
        mock_request.assert_called_once()

        fetch_ratings("tt0133093")
        mock_request.assert_called_once()

    @override_settings(OMDB_API_KEY="test-key", OMDB_DAILY_LIMIT=900)
    @patch("app.providers.omdb.services.api_request")
    def test_missing_omdb_values_stay_null(self, mock_request):
        """N/A and an absent critics score are None, not zero."""
        mock_request.return_value = {
            "Response": "True",
            "imdbRating": "N/A",
            "imdbVotes": "N/A",
            "Metascore": "N/A",
            "Ratings": [],
        }

        parsed = fetch_ratings("tt0000001")

        self.assertEqual(
            parsed,
            {
                "imdb": None,
                "imdb_votes": None,
                "rotten_tomatoes": None,
                "metacritic": None,
            },
        )

    @override_settings(OMDB_API_KEY="test-key", OMDB_DAILY_LIMIT=900)
    @patch("app.providers.omdb.services.api_request", return_value=OMDB_HIT)
    def test_cached_response_without_votes_is_fetched_again(self, mock_request):
        """A cache entry from before vote counts were stored is not reused."""
        cache.set(
            "omdb_ratings_tt0133093",
            {"imdb": Decimal("8.0"), "rotten_tomatoes": None, "metacritic": None},
        )

        parsed = fetch_ratings("tt0133093")

        self.assertEqual(parsed["imdb_votes"], 1234567)
        mock_request.assert_called_once()

    @override_settings(OMDB_API_KEY="test-key", OMDB_DAILY_LIMIT=1)
    @patch("app.providers.omdb.services.api_request", return_value=OMDB_HIT)
    def test_daily_limit_blocks_the_next_request(self, mock_request):
        """The second uncached title is refused once the daily cap is spent."""
        fetch_ratings("tt0000001")

        with self.assertRaises(OmdbBudgetError):
            fetch_ratings("tt0000002")

        mock_request.assert_called_once()

    @override_settings(OMDB_API_KEY="secret-omdb-key", OMDB_DAILY_LIMIT=900)
    @patch("app.providers.omdb.services.api_request")
    def test_http_401_limit_is_a_budget_error(self, mock_request):
        """A 401 whose body says the limit was reached stops the budget."""
        mock_request.side_effect = _http_error(
            401,
            {"Response": "False", "Error": "Request limit reached!"},
        )

        with (
            self.assertNoLogs("app.providers.omdb", level="WARNING"),
            self.assertRaises(OmdbBudgetError) as caught,
        ):
            fetch_ratings("tt0133093")

        self.assertNotIsInstance(caught.exception, OmdbAuthError)
        self.assertNotIn("secret-omdb-key", str(caught.exception))

    @override_settings(OMDB_API_KEY="secret-omdb-key", OMDB_DAILY_LIMIT=900)
    @patch("app.providers.omdb.services.api_request")
    def test_http_401_invalid_key_is_an_auth_error(self, mock_request):
        """A 401 for an invalid key is an auth error and the key is not logged."""
        mock_request.side_effect = _http_error(
            401,
            {"Response": "False", "Error": "Invalid API key!"},
        )

        with (
            self.assertLogs("app.providers.omdb", level="WARNING") as logs,
            self.assertRaises(OmdbAuthError),
        ):
            fetch_ratings("tt0133093")

        logged = "\n".join(logs.output)
        self.assertNotIn("secret-omdb-key", logged)
        self.assertNotIn("apikey", logged.lower())
        self.assertNotIn("omdbapi.com", logged)

    @override_settings(OMDB_API_KEY="secret-omdb-key", OMDB_DAILY_LIMIT=900)
    @patch("app.providers.omdb.services.api_request")
    def test_http_500_non_json_stays_a_request_error(self, mock_request):
        """A 500 with a non-JSON body stays retryable and does not log the key."""
        mock_request.side_effect = _http_error(500, b"<html>unavailable</html>")

        with (
            self.assertLogs("app.providers.omdb", level="WARNING") as logs,
            self.assertRaises(OmdbRequestError) as caught,
        ):
            fetch_ratings("tt0133093")

        self.assertNotIsInstance(caught.exception, OmdbBudgetError)
        logged = "\n".join(logs.output)
        self.assertNotIn("secret-omdb-key", logged)
        self.assertNotIn("apikey", logged.lower())


def _http_error(status, body):
    """Build an HTTPError whose response body is JSON or raw bytes."""
    response = requests.Response()
    response.status_code = status
    response.url = "https://www.omdbapi.com/?apikey=secret-omdb-key&i=tt0133093"
    if isinstance(body, bytes):
        response._content = body
    else:
        response._content = json.dumps(body).encode()
    response.encoding = "utf-8"
    message = f"{status} Client Error for url: {response.url}"
    return requests.HTTPError(message, response=response)
