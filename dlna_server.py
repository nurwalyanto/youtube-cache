import os
import re
import time
import socket
import struct
import threading
import uuid
import html as html_mod
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

from config import VIDEOS_DIR, SUBTITLES_DIR, THUMBNAILS_DIR, DLNA_PORT, SERVER_NAME
import metadata

MY_UUID = str(uuid.uuid4())
LOCAL_IP = None

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

LOCAL_IP = get_local_ip()
SSDP_MCAST = "239.255.255.250"
SSDP_PORT = 1900

def didl_escape(text):
    return html_mod.escape(text or "", quote=True)

class SSDPListener:
    def __init__(self, http_port):
        self.http_port = http_port
        self.running = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        mreq = struct.pack("4sl", socket.inet_aton(SSDP_MCAST), socket.INADDR_ANY)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        self.sock.bind(("0.0.0.0", SSDP_PORT))
        self.sock.settimeout(1.0)

    def build_response(self, st):
        return (
            "HTTP/1.1 200 OK\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            "DATE: {}\r\n"
            "EXT:\r\n"
            "LOCATION: http://{}:{}/description.xml\r\n"
            "SERVER: Linux/6.10, UPnP/1.0, {}/1.0\r\n"
            "ST: {}\r\n"
            "USN: uuid:{}::{}\r\n"
            "\r\n"
        ).format(
            time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
            LOCAL_IP, self.http_port, SERVER_NAME,
            st, MY_UUID, st
        )

    def run(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(1024)
                msg = data.decode("utf-8", errors="replace")
                st_match = re.search(r'ST:\s*(.+)', msg, re.I)
                if not st_match:
                    continue
                st = st_match.group(1).strip()
                if "ssdp:all" in st.lower():
                    self.sock.sendto(self.build_response("upnp:rootdevice").encode(), addr)
                    self.sock.sendto(self.build_response("urn:schemas-upnp-org:device:MediaServer:1").encode(), addr)
                elif "upnp:rootdevice" in st.lower():
                    self.sock.sendto(self.build_response("upnp:rootdevice").encode(), addr)
                elif "mediaserver" in st.lower():
                    self.sock.sendto(self.build_response("urn:schemas-upnp-org:device:MediaServer:1").encode(), addr)
            except socket.timeout:
                continue
            except:
                continue

    def stop(self):
        self.running = False

class DLNAHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _xml(self, body, status=200):
        body_b = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(body_b)))
        self.send_header("Server", f"{SERVER_NAME}/1.0 UPnP/1.0")
        self.send_header("Ext", "")
        self.end_headers()
        self.wfile.write(body_b)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/description.xml":
            desc = f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>{SERVER_NAME} ({HOSTNAME})</friendlyName>
    <manufacturer>YouTube Cache</manufacturer>
    <modelName>YouTube Cache DLNA</modelName>
    <modelNumber>1.0</modelNumber>
    <UDN>uuid:{MY_UUID}</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <controlURL>/upnp/control/content_directory</controlURL>
        <eventSubURL>/upnp/event/content_directory</eventSubURL>
        <SCPDURL>/scpd.xml</SCPDURL>
      </service>
    </serviceList>
  </device>
</root>"""
            self._xml(desc)
        elif parsed.path == "/scpd.xml":
            scpd = """<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>Browse</name>
      <argumentList>
        <argument><name>ObjectID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
        <argument><name>BrowseFlag</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>
        <argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
        <argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
        <argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
        <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
        <argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_UpdateID</name><dataType>ui4</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""
            self._xml(scpd)
        elif parsed.path.startswith("/thumbnail/"):
            vid = parsed.path.split("/thumbnail/")[1]
            for f in os.listdir(THUMBNAILS_DIR):
                if os.path.splitext(f)[0] == vid:
                    path = os.path.join(THUMBNAILS_DIR, f)
                    with open(path, "rb") as fh:
                        data = fh.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            self.send_response(404)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/upnp/control/content_directory":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            action = self.headers.get("SOAPAction", "").strip('"')
            if "Browse" in action:
                result_xml = self._handle_browse(body)
                self._xml(result_xml)
                return
        self.send_response(500)
        self.end_headers()

    def _handle_browse(self, body):
        obj_id = "0"
        flag = "BrowseMetadata"
        start = 0
        count = 100

        for m in re.finditer(r"<(\w+:)?ObjectID[^>]*>(.*?)</\1ObjectID>", body, re.DOTALL):
            obj_id = m.group(2).strip()
        for m in re.finditer(r"<(\w+:)?BrowseFlag[^>]*>(.*?)</\1BrowseFlag>", body, re.DOTALL):
            flag = m.group(2).strip()
        for m in re.finditer(r"<(\w+:)?StartingIndex[^>]*>(.*?)</\1StartingIndex>", body, re.DOTALL):
            start = int(m.group(2).strip())
        for m in re.finditer(r"<(\w+:)?RequestedCount[^>]*>(.*?)</\1RequestedCount>", body, re.DOTALL):
            count = int(m.group(2).strip())

        meta = metadata.load_metadata()
        didl_items = ""

        if flag == "BrowseDirectChildren":
            if obj_id == "0":
                for vid in meta[start:start + count]:
                    didl_items += self._video_item(vid)
            else:
                didl_items += self._video_item(next((v for v in meta if v["id"] == obj_id), {}))
        else:
            didl_items += self._container_item(meta)

        escaped = didl_escape(didl_items)
        nreturned = len(meta[start:start + count])
        total = len(meta)

        return f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
      <Result>{escaped}</Result>
      <NumberReturned>{nreturned}</NumberReturned>
      <TotalMatches>{total}</TotalMatches>
      <UpdateID>0</UpdateID>
    </u:BrowseResponse>
  </s:Body>
</s:Envelope>"""

    def _video_item(self, vid):
        vid_id = vid.get("id", "")
        title = didl_escape(vid.get("title", "Unknown"))
        stream_url = f"http://{LOCAL_IP}:{DLNA_PORT}/stream/{vid_id}"
        thumb_url = f"http://{LOCAL_IP}:{DLNA_PORT}/thumbnail/{vid_id}"

        video_path = None
        for f in os.listdir(VIDEOS_DIR):
            if os.path.splitext(f)[0] == vid_id:
                video_path = os.path.join(VIDEOS_DIR, f)
                break
        size = os.path.getsize(video_path) if video_path else 0

        return f"""<item id="{didl_escape(vid_id)}" parentID="0" restricted="1">
  <dc:title>{title}</dc:title>
  <upnp:class>object.item.videoItem</upnp:class>
  <upnp:albumArtURI>{didl_escape(thumb_url)}</upnp:albumArtURI>
  <res protocolInfo="http-get:*:video/mp4:DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000" size="{size}">{didl_escape(stream_url)}</res>
</item>"""

    def _container_item(self, meta):
        return f"""<container id="0" parentID="-1" restricted="1">
  <dc:title>{didl_escape(SERVER_NAME)}</dc:title>
  <upnp:class>object.container.storageFolder</upnp:class>
  <childCount>{len(meta)}</childCount>
</container>"""

class DLMAServer:
    def __init__(self, http_port=DLNA_PORT):
        self.http_port = http_port
        self.ssdp = SSDPListener(http_port)
        self.httpd = None

    def start(self):
        threading.Thread(target=self.ssdp.run, daemon=True, name="ssdp").start()
        self.httpd = HTTPServer(("0.0.0.0", self.http_port), DLNAHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="dlna-http").start()

    def stop(self):
        self.ssdp.stop()
        if self.httpd:
            self.httpd.shutdown()
