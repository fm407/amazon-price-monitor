import httpx
import pytest

from app.amazon import InvalidProductInput, assert_public_host, parse_product_input

PUBLIC = lambda host: ["52.94.236.248"]  # noqa: E731


def short_link_client(routes: dict[str, str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        target = routes.get(str(request.url))
        return httpx.Response(301, headers={"location": target}) if target else httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "text",
    [
        "B0DEMOGPU1",
        "b0demogpu1",
        "https://www.amazon.com/dp/B0DEMOGPU1",
        "https://www.amazon.com/ASRock-Intel-Arc-Pro/dp/B0DEMOGPU1/ref=sr_1_3?crid=X&tag=aff-20&th=1",
        "https://www.amazon.com/gp/product/B0DEMOGPU1?psc=1",
        "https://smile.amazon.com/gp/aw/d/B0DEMOGPU1",
        "http://amazon.com/dp/B0DEMOGPU1#customerReviews",
        "amazon.com/dp/B0DEMOGPU1",
    ],
)
def test_extracts_asin_and_builds_canonical_url(text):
    identity = parse_product_input(text)
    assert identity.asin == "B0DEMOGPU1"
    assert identity.marketplace == "US"
    assert identity.url == "https://www.amazon.com/dp/B0DEMOGPU1"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not a link",
        "B0SHORT",
        "https://www.example.com/dp/B0DEMOGPU1",
        "https://www.amazon.com.evil.example/dp/B0DEMOGPU1",
        "https://www.amazon.com/s?k=gpu",
        "ftp://www.amazon.com/dp/B0DEMOGPU1",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://user:pw@www.amazon.com/dp/B0DEMOGPU1",
        "https://www.amazon.com:8443/dp/B0DEMOGPU1",
        "https://www.amazon.de/dp/B0DEMOGPU1",
        "http://localhost/dp/B0DEMOGPU1",
        "http://127.0.0.1/dp/B0DEMOGPU1",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/dp/B0DEMOGPU1",
        "x" * 3000,
    ],
)
def test_rejects_unsupported_or_unsafe_input(text):
    with pytest.raises(InvalidProductInput):
        parse_product_input(text)


def test_resolves_short_link_through_allowlisted_redirects():
    client = short_link_client(
        {
            "https://a.co/d/abc123": "https://amzn.to/xyz",
            "https://amzn.to/xyz": "https://www.amazon.com/dp/B0DEMOSSD1?tag=t-20",
        }
    )
    identity = parse_product_input("https://a.co/d/abc123", client=client, dns_lookup=PUBLIC)
    assert identity.url == "https://www.amazon.com/dp/B0DEMOSSD1"


@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254/latest/meta-data/",
        "https://internal.example/dp/B0DEMOSSD1",
        "http://www.amazon.com/dp/B0DEMOSSD1",  # downgrade to http
        "file:///etc/passwd",
        "https://127.0.0.1/dp/B0DEMOSSD1",
    ],
)
def test_short_link_redirect_to_unsafe_destination_is_rejected(target):
    client = short_link_client({"https://a.co/d/abc123": target})
    with pytest.raises(InvalidProductInput):
        parse_product_input("https://a.co/d/abc123", client=client, dns_lookup=PUBLIC)


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "192.168.1.10", "172.16.0.1", "169.254.169.254", "::1", "fd00::1", "0.0.0.0"],
)
def test_short_link_host_resolving_to_non_public_address_is_rejected(address):
    client = short_link_client({"https://a.co/d/abc123": "https://www.amazon.com/dp/B0DEMOSSD1"})
    with pytest.raises(InvalidProductInput):
        parse_product_input("https://a.co/d/abc123", client=client, dns_lookup=lambda host: [address])
    with pytest.raises(InvalidProductInput):
        assert_public_host("a.co", lambda host: ["52.94.236.248", address])


def test_short_link_redirect_loop_stops_after_limit():
    client = short_link_client({"https://a.co/d/loop": "https://a.co/d/loop"})
    with pytest.raises(InvalidProductInput, match="too many"):
        parse_product_input("https://a.co/d/loop", client=client, dns_lookup=PUBLIC)


def test_http_short_link_is_rejected_without_any_request():
    client = short_link_client({})
    with pytest.raises(InvalidProductInput, match="https"):
        parse_product_input("http://a.co/d/abc123", client=client, dns_lookup=PUBLIC)
