from unittest.mock import MagicMock, patch

import pytest
from wrappers.jira import Jira, JiraForbiddenError


def _resp(status=200, payload=None):
    resp = MagicMock(status_code=status, text="body")
    resp.json.return_value = payload or {}
    return resp


@pytest.fixture
def jira():
    with (
        patch(
            "wrappers.jira.JiraAuth",
            return_value=MagicMock(email="bot@redhat.com", token="t"),
        ),
        patch(
            "wrappers.jira.config.get_jira_url",
            return_value="https://redhat.atlassian.net/",
        ),
        patch(
            "wrappers.jira.config.get_timeouts",
            return_value={"http_seconds": 30},
        ),
    ):
        client = Jira()
    client.session = MagicMock()
    return client


def test_search_follows_next_page_token(jira):
    jira.session.request.side_effect = [
        _resp(payload={"issues": [{"key": "MTV-1"}], "nextPageToken": "p2",
                       "isLast": False}),
        _resp(payload={"issues": [{"key": "MTV-2"}], "isLast": True}),
    ]
    issues = jira.search("project = MTV", ["summary"])
    assert [i["key"] for i in issues] == ["MTV-1", "MTV-2"]
    second = jira.session.request.call_args_list[1]
    assert second.args == (
        "POST",
        "https://redhat.atlassian.net/rest/api/3/search/jql",
    )
    assert second.kwargs["json"]["nextPageToken"] == "p2"


def test_search_keeps_paging_when_is_last_missing(jira):
    # A short page with a token but no isLast must not end the search early.
    jira.session.request.side_effect = [
        _resp(payload={"issues": [{"key": "A"}], "nextPageToken": "p2"}),
        _resp(payload={"issues": [{"key": "B"}]}),
    ]
    issues = jira.search("project = OCPBUGS", ["summary"])
    assert [i["key"] for i in issues] == ["A", "B"]


def test_search_stops_when_is_last_true(jira):
    jira.session.request.return_value = _resp(
        payload={"issues": [{"key": "A"}], "nextPageToken": "x", "isLast": True}
    )
    assert len(jira.search("project = OCPBUGS", ["summary"])) == 1
    jira.session.request.assert_called_once()


def test_forbidden_raises_distinct_error(jira):
    jira.session.request.return_value = _resp(status=403)
    with pytest.raises(JiraForbiddenError):
        jira.get_comments("MTV-6789")


def test_add_comment_uses_v2_wiki_markup(jira):
    jira.session.request.return_value = _resp(status=201)
    jira.add_comment("MTV-1", "*bold*")
    call = jira.session.request.call_args
    assert call.args == (
        "POST",
        "https://redhat.atlassian.net/rest/api/2/issue/MTV-1/comment",
    )
    assert call.kwargs["json"] == {"body": "*bold*"}


def test_add_labels_uses_update_add(jira):
    jira.session.request.return_value = _resp(status=204)
    jira.add_labels("MTV-1", ["triage:ready-to-close"])
    assert jira.session.request.call_args.kwargs["json"] == {
        "update": {"labels": [{"add": "triage:ready-to-close"}]}
    }
