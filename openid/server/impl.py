from openid.server import interface
from openid.server.trustroot import TrustRoot
from openid import oidutil
from openid import cryptutil
from openid.dh import DiffieHellman

_signed_fields = ['mode', 'identity', 'return_to']

class OpenIDServerImpl(object):
    def __init__(self, server_url, internal_store, external_store):
        self.url = server_url
        self.istore = internal_store
        self.estore = external_store

    def getAuthData(self, args):
        trust_root = args.get('openid.trust_root')
        identity = args.get('openid.identity')
        return identity, trust_root

    def processGet(self, authorized, args):
        identity = args.get('openid.identity')
        if identity is None:
            return self._getErr(args, 'No identity specified')

        trust_root = args.get('openid.trust_root')
        tr = TrustRoot.parse(trust_root)
        if tr is None:
            return self._getErr(args, 'Malformed trust_root: %s' % trust_root)

        return_to = args.get('openid.return_to')
        if return_to is None:
            return self._getErr(args, 'No return_to URL specified')

        if not tr.validateURL(return_to):
            return self._getErr(
                args, 'return_to(%s) not valid against trust_root(%s)' % (
                return_to, trust_root))

        assoc_handle = args.get('openid.assoc_handle')
        mode = args.get('openid.mode')

        if not authorized:
            if mode == 'checkid_immediate':
                nargs = dict(args)
                nargs['openid.mode'] = 'checkid_setup'
                return interface.REDIRECT, oidutil.appendArgs(self.url, nargs)

            elif mode == 'checkid_setup':
                ret = oidutil.appendArgs(self.url, args)
                can = oidutil.appendArgs(return_to, {'openid.mode': 'cancel'})

                return interface.DO_AUTH, (ret, can)

            else:
                return self._getErr(
                    args, 'open.mode (%r) not understood' % mode)

        reply = {
            'openid.mode': 'id_res',
            'openid.return_to': return_to,
            'openid.identity': identity,
            }

        if assoc_handle:
            assoc = self.estore.lookup(assoc_handle, 'HMAC-SHA1')

            # fall back to dumb mode if assoc_handle not found,
            # and send the consumer an invalidate_handle message
            if assoc is None or assoc.expiresIn <= 0:
                if assoc is not None and assoc.expiresIn <= 0:
                    self.estore.remove(assoc.handle)
                assoc = self.istore.get('HMAC-SHA1')
                reply['openid.invalidate_handle'] = assoc_handle
        else:
            assoc = self.istore.get('HMAC-SHA1')

        reply.update({
            'openid.assoc_handle': assoc.handle,
            })

        signed, sig = cryptutil.signReply(reply, assoc.secret, _signed_fields)

        reply.update({
            'openid.signed': signed,
            'openid.sig': sig,
            })

        return interface.REDIRECT, oidutil.appendArgs(return_to, reply)

    def processPost(self, args):
        try:
            mode = args['openid.mode']
            if mode == 'associate':
                return self._associate(args)
            elif mode == 'check_authentication':
                return self._checkAuth(args)
            else:
                return self._postErr('Unrecognized openid.mode (%r)' % mode)
        except KeyError, e:
            return self._postErr(
                'Necessary openid argument (%r) missing.' % e[0])

    def _associate(self, args):
        reply = {}
        assoc_type = args.get('openid.assoc_type', 'HMAC-SHA1')
        assoc = self.estore.get(assoc_type)

        if assoc is None:
            return self._postErr(
                'unable to create an association for type %r' % assoc_type)

        reply.update({
            'assoc_type': 'HMAC-SHA1',
            'assoc_handle': assoc.handle,
            'expires_in': assoc.expiresIn,
            })

        session_type = args.get('openid.session_type')
        if session_type:
            if session_type == 'DH-SHA1':
                p = args['openid.dh_modulus']
                g = args['openid.dh_gen']
                consumer_public = args['openid.dh_consumer_public']

                dh = DiffieHellman.fromBase64(p, g)

                cpub = cryptutil.base64ToLong(consumer_public)
                dh_shared = dh.decryptKeyExchange(cpub)
                mac_key = cryptutil.strxor(
                    assoc.secret, cryptutil.sha1(
                    cryptutil.longToBinary(dh_shared)))
                spub = dh.createKeyExchange()

                reply.update({
                    'session_type': session_type,
                    'dh_server_public': cryptutil.longToBase64(spub),
                    'enc_mac_key': oidutil.toBase64(mac_key),
                    })
            else:
                return self._postErr('session_type must be DH-SHA1')
        else:
            reply['mac_key'] = oidutil.toBase64(assoc.secret)

        print (reply, mac_key)

        return interface.OK, oidutil.dictToKV(reply)

    def _checkAuth(self, args):
        assoc = self.istore.lookup(args['openid.assoc_handle'], 'HMAC-SHA1')

        if assoc is None:
            return self._postErr(
                'no secret found for %r' % args['openid.assoc_handle'])

        reply = {}
        if assoc.expiresIn > 0:
            dat = dict(args)
            dat['openid.mode'] = 'id_res'

            signed_fields = args['openid.signed'].strip().split(',')
            _, v_sig = cryptutil.signReply(dat, assoc.secret, signed_fields)

            if v_sig == args['openid.sig']:
                self.estore.remove(args['openid.assoc_handle'])
                is_valid = 'true'

                invalidate_handle = args.get('openid.invalidate_handle')
                if invalidate_handle:
                    if not self.estore.lookup(invalidate_handle, 'HMAC-SHA1'):
                        reply['invalidate_handle'] = invalidate_handle
            else:
                is_valid = 'false'

        else:
            self.istore.remove(args['openid.assoc_handle'])
            is_valid = 'false'

        reply['is_valid'] = is_valid
        return interface.OK, oidutil.dictToKV(reply)

    def _getErr(self, args, msg):
        return_to = args.get('openid.return_to')
        if return_to:
            err = {
                'openid.mode': 'error',
                'openid.error': msg
                }
            return interface.REDIRECT, oidutil.appendArgs(return_to, err)
        else:
            return interface.ERROR, msg

    def _postErr(self, msg):
        return interface.ERROR, oidutil.dictToKV({'error': msg})
