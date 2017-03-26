
import falcon
import logging
import ipaddress
from xattr import getxattr, listxattr
from datetime import datetime
from certidude import config, authority
from certidude.decorators import serialize

logger = logging.getLogger("api")

class AttributeResource(object):
    @serialize
    def on_get(self, req, resp, cn):
        """
        Return extended attributes stored on the server.
        This not only contains tags and lease information,
        but might also contain some other sensitive information.
        """
        path, buf, cert = authority.get_signed(cn)

        attribs = dict()
        for key in listxattr(path):
            if not key.startswith("user."):
                continue
            value = getxattr(path, key)
            current = attribs
            if "." in key:
                namespace, key = key.rsplit(".", 1)
                for component in namespace.split("."):
                    if component not in current:
                        current[component] = dict()
                    current = current[component]
            current[key] = value

        whitelist = attribs.get("user").get("address")

        if req.context.get("remote_addr") != whitelist:
            logger.info("Attribute access denied from %s, expected %s for %s",
                req.context.get("remote_addr"),
                whitelist,
                cn)
            raise falcon.HTTPForbidden("Forbidden",
                "Attributes only accessible to the machine")

        return attribs
