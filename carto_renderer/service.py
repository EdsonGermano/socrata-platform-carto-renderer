#!/usr/bin/env python
"""
Service to render pngs from vector tiles using Carto CSS.
"""
import mapnik
import mapbox_vector_tile
from tornado.ioloop import IOLoop
from tornado.web import Application, RedirectHandler, RequestHandler, url

import argparse
import json
import logging
import collections
import base64
import logging.config

from carto_renderer.errors import BadRequest, JsonKeyError, ServiceError
from carto_renderer.version import SEMANTIC

__package__ = 'carto_renderer'  # pylint: disable=redefined-builtin

GEOM_TYPES = {
    1: 'POINT',
    2: 'LINE_STRING',
    3: 'POLYGON'
}

# Variables for Vector Tiles.
BASE_ZOOM = 29
TILE_ZOOM_FACTOR = 16
LOG_ENV = {'X-Socrata-RequestId': None}


def build_wkt(geom_code, geometries):
    """
    Build a Well Known Text of the appropriate type.

    Returns None on failure.
    """
    logger = logging.getLogger(__package__)
    geom_type = GEOM_TYPES.get(geom_code, 'UNKNOWN')

    def collapse(coords):
        """
        Helper, collapses lists into strings with appropriate parens.
        """
        if len(coords) < 1:
            return '()'

        first = coords[0]
        if not isinstance(first, collections.Iterable):
            return ' '.join([str(c / TILE_ZOOM_FACTOR) for c in coords])
        else:
            return '(' + (','.join([collapse(c) for c in coords])) + ')'

    collapsed = collapse(geometries)

    if geom_type == 'UNKNOWN':
        logger.warn(u'Unknown geometry code: %s', geom_code, extra=LOG_ENV)
        return None

    if geom_type != 'POINT':
        collapsed = '(' + collapsed + ')'

    return geom_type + collapsed


def render_png(tile, zoom, xml):
    # mapnik is installed in a non-standard way.
    # It confuses pylint.
    # pylint: disable=no-member
    """
    Render the tile for the given zoom
    """
    logger = logging.getLogger(__package__)
    ctx = mapnik.Context()

    map_tile = mapnik.Map(256, 256)
    # scale_denom = 1 << (BASE_ZOOM - int(zoom or 1))
    # scale_factor = scale_denom / map_tile.scale_denominator()

    # map_tile.zoom(scale_factor)  # TODO: Is overriden by zoom_to_box.
    mapnik.load_map_from_string(map_tile, xml)
    map_tile.zoom_to_box(mapnik.Box2d(0, 0, 255, 255))

    for (name, features) in tile.items():
        name = name.encode('ascii', 'ignore')
        source = mapnik.MemoryDatasource()
        map_layer = mapnik.Layer(name)
        map_layer.datasource = source

        for feature in features:
            wkt = build_wkt(feature['type'], feature['geometry'])
            logger.debug('wkt: %s', wkt, extra=LOG_ENV)
            feat = mapnik.Feature(ctx, 0)
            if wkt:
                feat.add_geometries_from_wkt(wkt)
            source.add_feature(feat)

        map_layer.styles.append(name)
        map_tile.layers.append(map_layer)

    image = mapnik.Image(map_tile.width, map_tile.height)
    mapnik.render(map_tile, image)

    return image.tostring('png')


class BaseHandler(RequestHandler):
    # pylint: disable=abstract-method
    """
    Convert ServiceErrors to HTTP errors.
    """
    def extract_jbody(self):
        """
        Extract the json body from self.request.
        """
        logger = logging.getLogger(__package__ +
                                   '.' +
                                   self.__class__.__name__)

        request_id = self.request.headers.get('x-socrata-requestid', '')
        LOG_ENV['X-Socrata-RequestId'] = request_id

        content_type = self.request.headers.get('content-type', '')
        if not content_type.lower().startswith('application/json'):
            message = 'Invalid Content-Type: "{ct}"; ' + \
                      'expected "application/json"'
            logger.warn('Invalid Content-Type: "%s"',
                        content_type,
                        extra=LOG_ENV)
            raise BadRequest(message.format(ct=content_type))

        body = self.request.body

        try:
            jbody = json.loads(body)
        except StandardError:
            logger.warn('Invalid JSON', extra=LOG_ENV)
            raise BadRequest('Could not parse JSON.', body)
        return jbody

    def _handle_request_exception(self, err):
        """
        Convert ServiceErrors to HTTP errors.
        """
        logger = logging.getLogger(__package__ +
                                   '.' +
                                   self.__class__.__name__)

        payload = {}
        logger.exception(err, extra=LOG_ENV)
        if isinstance(err, ServiceError):
            status_code = err.status_code
            if err.request_body:
                payload['request_body'] = err.request_body
        else:
            status_code = 500

        payload['resultCode'] = status_code
        payload['message'] = err.message

        self.clear()
        self.set_status(status_code)
        self.write(json.dumps(payload))
        self.finish()


class VersionHandler(BaseHandler):
    # pylint: disable=abstract-method
    """
    Return the version of the service, currently hardcoded.
    """
    version = {'health': 'alive', 'version': SEMANTIC}

    def get(self):
        """
        Return the version of the service, currently hardcoded.
        """
        logger = logging.getLogger(__package__ +
                                   '.' +
                                   self.__class__.__name__)
        logger.info('Alive!', extra=LOG_ENV)
        self.write(VersionHandler.version)
        self.finish()


class RenderHandler(BaseHandler):
    # pylint: disable=abstract-method
    """
    Actually render the png.

    Expects a JSON blob with 'style', 'zoom', and 'bpbf' values.
    """
    keys = ['bpbf', 'zoom', 'style']

    def post(self):
        """
        Actually render the png.

        Expects a JSON blob with 'style', 'zoom', and 'bpbf' values.
        """
        logger = logging.getLogger(__package__ +
                                   '.' +
                                   self.__class__.__name__)

        jbody = self.extract_jbody()

        if not all([k in jbody for k in self.keys]):
            logger.warn('Invalid JSON: %s', jbody, extra=LOG_ENV)
            raise JsonKeyError(self.keys, jbody)
        else:
            try:
                zoom = int(jbody['zoom'])
            except:
                logger.warn('Invalid JSON; zoom must be an integer: %s',
                            jbody,
                            extra=LOG_ENV)
                raise BadRequest('"zoom" must be an integer.',
                                 request_body=jbody)
            pbf = base64.b64decode(jbody['bpbf'])
            tile = mapbox_vector_tile.decode(pbf)
            xml = jbody['style']  # TODO: Actually render!

            logger.info('zoom: %d, len(pbf): %d, len(xml): %d',
                        zoom,
                        len(pbf),
                        len(xml),
                        extra=LOG_ENV)
            self.write(render_png(tile, zoom, xml))
            self.finish()


def main():  # pragma: no cover
    """
    Actually fire up the web server.

    Listens on 4096.
    """
    parser = argparse.ArgumentParser(
        description='A rendering service for vector tiles using Mapnik.')
    parser.add_argument('--log-config-file',
                        dest='log_config_file',
                        default='logging.ini',
                        help='Config file for `logging.config`')
    args = parser.parse_args()
    logging.config.fileConfig(args.log_config_file)

    routes = [
        url(r'/', RedirectHandler, {'url': '/version'}),
        url(r'/version', VersionHandler),
        url(r'/render', RenderHandler),
    ]

    app = Application(routes)
    app.listen(4096)
    logger = logging.getLogger(__package__)
    logger.info('Listening on localhost:4096...', extra=LOG_ENV)
    IOLoop.current().start()

if __name__ == '__main__':  # pragma: no cover
    main()
