import synapse.lib.gis as s_gis
import synapse.lib.types as s_types
import synapse.lib.syntax as s_syntax

from synapse.lib.module import CoreModule

class LatLongType(s_types.DataType):

    def norm(self, valu, oldval=None):

        lat, lon = valu.split(',', 1)

        try:
            latv = float(lat.strip())
            lonv = float(lon.strip())
        except Exception as e:
            self._raiseBadValu(valu, mesg='Invalid float format')

        #TODO eventually support minutes / sec and N/S E/W syntax

        if 90.0 < latv < -90.0:
            self._raiseBadValu(valu, mesg='Latitude may only be -90.0 to 90.0')

        if 180.0 < lonv < -180.0:
            self._raiseBadValu(valu, mesg='Longitude may only be -180.0 to 180.0')

        norm = '%s,%s' % (latv, lonv)
        return norm, {}

units = {
    'mm': 1,
    'cm': 10,

    'm': 1000,
    'meters': 1000,

    'km': 1000000,
}

class DistType(s_types.DataType):

    def norm(self, valu, oldval=None):

        if type(valu) == str:
            return self._norm_str(valu)
        return valu, {}

    def _norm_str(self, text):

        valu, off = s_syntax.parse_float(text, 0)
        unit, off = s_syntax.nom(text, off, s_syntax.alphaset)

        mult = units.get(unit.lower())
        if mult is None:
            raise SyntaxError('invalid units: %s' % (unit,))

        return valu * mult, {}

class GeoMod(CoreModule):

    #def postCoreModule(self):
        #self.core.setOperFunc('geo:near'

    @staticmethod
    def getBaseModels():
        modl = {
            'types': (
                ('geo:place', {'subof': 'guid', 'alias': 'geo:place:alias', 'doc': 'A GUID for a specific place'}),
                ('geo:alias', {'subof': 'str:lwr', 'regex': '^[0-9a-z]+$', 'doc': 'An alias for the place GUID', 'ex': 'foobar'}),

                ('geo:dist', {'ctor': 'synapse.models.geospace.DistType',
                    'doc': 'A geographic distance', 'ex': '10 km'}),

                ('geo:latlong', {'ctor': 'synapse.models.geospace.LatLongType',
                    'doc': 'A Lat/Long string specifying a point on earth'}),
            ),

            'forms': (

                ('geo:place', {'ptype': 'geo:place'}, [
                    ('alias', {'ptype': 'geo:alias'}),
                    ('name', {'ptype': 'str', 'lower': 1, 'doc': 'The name of the place'}),
                    ('latlong', {'ptype': 'geo:latlong', 'defval': '??', 'doc': 'The location of the place'}),
                ]),

                ('geo:loctime', {'ptype': 'guid'}, [
                    ('node', {'ptype': 'propvalu', 'doc': 'The node with location in geo/time'}),
                    ('time', {'ptype': 'time', 'doc': 'The time the node was observed at location'}),
                ]),
            ),

            'props': (
                ('node:loc', {'ptype': 'geo:latlong', 'doc': 'The current geospacial location of the node'}),
            ),

        }
        name = 'geo'
        return ((name, modl), )
