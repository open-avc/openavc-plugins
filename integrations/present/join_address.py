"""Where a guest reaches this server: the address on the connect card, the host
the media sidecar advertises to browsers, and the origin a local kiosk window
opens.

All three read the same facts about the platform's own listeners (its HTTP
port, whether port 80 answers, whether HTTPS is on and on which port, whether
plain HTTP is redirected to it), read from the platform's system config the
same way its own setup screen and /api/system/tls-status read them. Each
reader falls back to "off" or the default port when the platform is not
importable (unit tests) or its config surprises, so a reader never raises.

When the platform gains a listener setting the card should follow, the reader
and the rule that uses it both live here.
"""

import ipaddress
import socket
import time

# The auto-detected join address is re-resolved at most this often.
_IP_CACHE_SECONDS = 60.0


class JoinAddress:
    """The join address rules for one plugin instance.

    Holds only the auto-detected LAN address and when it was detected; the
    explicit address comes from the plugin's config on every call.
    """

    def __init__(self, guest_alias):
        self._guest_alias = guest_alias
        self._detected = ("", 0.0)  # (auto-detected address, at monotonic)

    # ──── The platform's listeners ────

    @staticmethod
    def http_port():
        try:
            from openavc.system_config import get_system_config

            return int(get_system_config().get("network", "http_port"))
        except Exception:
            return 8080

    @staticmethod
    def port80_active():
        """Whether port 80 answers for the platform, so a typed http URL can
        drop the port. Set only once a listener actually bound it (the
        port-80 listener is best effort); False on a platform without it."""
        try:
            from openavc import runtime_flags

            return bool(runtime_flags.port80_active)
        except Exception:
            return False

    @staticmethod
    def tls_state():
        """(enabled, port) for the instance's HTTPS listener.

        TLS toggles take a server restart to apply, so config and the running
        listener only disagree in the brief toggled-but-not-restarted window
        the Settings UI already warns about.
        """
        try:
            from openavc.system_config import get_system_config

            cfg = get_system_config()
            if cfg.get("tls", "enabled"):
                return True, int(cfg.get("tls", "port") or 8443)
        except Exception:
            pass
        return False, 0

    @staticmethod
    def redirect_http_enabled():
        """Whether the platform redirects plain-HTTP requests to the TLS
        listener (tls.redirect_http)."""
        try:
            from openavc.system_config import get_system_config

            return bool(get_system_config().get("tls", "redirect_http"))
        except Exception:
            return False

    # ──── The host ────

    def detect_local_ip(self):
        """Best-guess LAN address via the UDP-connect trick (no packets sent).

        Same method the platform's mDNS advertiser uses. Multi-NIC/VLAN
        installs where this guesses wrong set the join_address config field.
        """
        ip, at = self._detected
        if ip and time.monotonic() - at < _IP_CACHE_SECONDS:
            return ip
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.settimeout(0.5)
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
            finally:
                s.close()
        except OSError:
            ip = "127.0.0.1"
        self._detected = (ip, time.monotonic())
        return ip

    def host(self, configured):
        """The host guests reach this instance at: the join_address config
        when set, else the auto-detected LAN address. One resolution point
        for the join URL and the sidecar's advertised WebRTC host."""
        return (configured or "").strip() or self.detect_local_ip()

    def advertised_ips(self, host, log=None):
        """IP literals to advertise as WebRTC host candidates for ``host``.

        Only IP literals go into webrtcAdditionalHosts: MediaMTX resolves a
        hostname entry server-side on every connect and treats a lookup
        failure as fatal for that session, so one bad DNS day would kill all
        publishing. A join_address hostname is resolved here once at render
        time instead; if it doesn't resolve from this machine, the detected
        LAN address stands in (loudly) so guests can still connect.
        """
        try:
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(host, None, family=socket.AF_INET)
            ips = list(dict.fromkeys(info[4][0] for info in infos))
            if ips:
                return ips
        except OSError:
            pass
        detected = self.detect_local_ip()
        if log is not None:
            log(
                f"join_address '{host}' does not resolve from this machine; "
                f"advertising the detected LAN address {detected} to WebRTC "
                "clients instead",
                "warning",
            )
        return [detected]

    # ──── The addresses ────

    async def url(self, host, cert_status):
        """The address a guest types, exactly as the connect cards show it.

        ``cert_status`` is awaited for the cloud certificate's status, only
        when HTTPS is on.

        With HTTPS on, the card shows the full https URL — accurate beats
        short. A scheme-less <host>:8080 relies on the platform's redirect,
        and browsers with automatic HTTPS upgrades ("Always use secure
        connections") rewrite it to https on the SAME port before the
        redirect can answer, landing a TLS handshake on the plain-HTTP
        listener (observed on the bench: an SSL error for the guest). A
        scheme-less <host>:8443 is just as broken the other way around —
        typed addresses default to http. Only the explicit https form works
        in every browser with no settings. With TLS off, the short
        <host>:8080 form works everywhere and stays.

        One tier sits above all of that: when a cloud-issued trusted
        certificate is installed AND the platform's http→https redirect is
        on, the card shows the explicit http URL instead. The redirect sends
        the guest to the certified hostname (a real CA certificate), so they
        land on a green-lock page with no browser warning at all — the
        direct https form would serve the self-signed cert and put the
        interstitial right back.

        Either http form drops the port when the platform has port 80
        answering (HTTP itself on 80, or its port-80 listener), the same rule
        the platform's own setup screen follows.
        """
        tls_enabled, tls_port = self.tls_state()
        if tls_enabled:
            cert = await cert_status()
            if cert == "installed" and self.redirect_http_enabled():
                return f"http://{self._http_host(host)}/{self._guest_alias}"
            hostport = host if tls_port == 443 else f"{host}:{tls_port}"
            return f"https://{hostport}/{self._guest_alias}"
        return f"{self._http_host(host)}/{self._guest_alias}"

    def _http_host(self, host):
        """``host`` as a typed plain-HTTP address: the port dropped when it is 80."""
        port = 80 if self.port80_active() else self.http_port()
        return host if port == 80 else f"{host}:{port}"

    def local_origin(self):
        """The origin a kiosk window on this host opens: localhost, scheme-
        qualified the same way the join URL is. With TLS on it goes straight
        to the HTTPS listener rather than through the redirect hop
        (--allow-insecure-localhost keeps the self-signed interstitial away).
        """
        tls_enabled, tls_port = self.tls_state()
        if tls_enabled:
            return f"https://127.0.0.1:{tls_port}"
        return f"http://127.0.0.1:{self.http_port()}"
