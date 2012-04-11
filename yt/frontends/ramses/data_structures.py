"""
RAMSES-specific data structures

Author: Matthew Turk <matthewturk@gmail.com>
Affiliation: UCSD
Homepage: http://yt-project.org/
License:
  Copyright (C) 2010-2011 Matthew Turk.  All Rights Reserved.

  This file is part of yt.

  yt is free software; you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation; either version 3 of the License, or
  (at your option) any later version.

  This program is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.

  You should have received a copy of the GNU General Public License
  along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import numpy as na
import stat
import weakref

from yt.funcs import *
from yt.data_objects.grid_patch import \
      AMRGridPatch
from yt.geometry.oct_geometry_handler import \
    OctreeGeometryHandler
from yt.geometry.geometry_handler import \
    GeometryHandler, YTDataChunk
from yt.data_objects.static_output import \
    StaticOutput

from .fields import RAMSESFieldInfo, KnownRAMSESFields
from .definitions import ramses_header
from yt.utilities.definitions import \
    mpc_conversion
from yt.utilities.amr_utils import \
    get_box_grids_level
from yt.utilities.io_handler import \
    io_registry
from yt.data_objects.field_info_container import \
    FieldInfoContainer, NullFunc
import yt.utilities.fortran_utils as fpu
from yt.geometry.oct_container import \
    RAMSESOctreeContainer

class RAMSESDomainFile(object):
    _last_mask = None
    _last_selector_id = None

    def __init__(self, pf, domain_id):
        self.pf = pf
        self.domain_id = domain_id
        num = os.path.basename(pf.parameter_filename).split("."
                )[0].split("_")[1]
        basename = "%s/%%s_%s.out%05i" % (
            os.path.dirname(pf.parameter_filename),
            num, domain_id)
        for t in ['grav', 'hydro', 'part', 'amr']:
            setattr(self, "%s_fn" % t, basename % t)
        self._read_amr_header()

    def _read_amr_header(self):
        hvals = {}
        f = open(self.amr_fn, "rb")
        for header in ramses_header(hvals):
            hvals.update(fpu.read_attrs(f, header))
        # That's the header, now we skip a few.
        hvals['numbl'] = na.array(hvals['numbl']).reshape(
            (hvals['nlevelmax'], hvals['ncpu']))
        fpu.skip(f)
        if hvals['nboundary'] > 0:
            fpu.skip(f, 2)
            ngridbound = fpu.read_vector(f, 'i')
        free_mem = fpu.read_attrs(f, (('free_mem', 5, 'i'), ) )
        ordering = fpu.read_vector(f, 'c')
        fpu.skip(f, 4)
        # Now we're at the tree itself
        # Now we iterate over each level and each CPU.
        self.amr_header = hvals
        self.amr_offset = f.tell()
        self.local_oct_count = hvals['numbl'][:, self.domain_id - 1].sum()

    def _read_amr(self, oct_handler):
        f = open(self.amr_fn, "rb")
        f.seek(self.amr_offset)
        mylog.debug("Reading domain AMR % 4i (%0.3e)",
            self.domain_id, self.local_oct_count)
        def _ng(c, l):
            if c < self.amr_header['ncpu']:
                ng = self.amr_header['numbl'][l, c]
            else:
                ng = ngridbound[c - self.amr_header['ncpu'] +
                                self.amr_header['nboundary']*l]
            return ng
        for level in range(self.amr_header['nlevelmax']):
            # Easier if do this 1-indexed
            for cpu in range(self.amr_header['nboundary'] + self.amr_header['ncpu']):
                ng = _ng(cpu, level)
                if ng == 0: continue
                ind = fpu.read_vector(f, "I").astype("int64")
                #print level, cpu, ind.min(), ind.max(), ind.size
                fpu.skip(f, 2)
                pos = na.empty((ng, 3), dtype='float64')
                pos[:,0] = fpu.read_vector(f, "d")
                pos[:,1] = fpu.read_vector(f, "d")
                pos[:,2] = fpu.read_vector(f, "d")
                pos *= self.pf.domain_width
                #pos += self.parameter_file.domain_left_edge
                #print pos.min(), pos.max()
                parents = fpu.read_vector(f, "I")
                fpu.skip(f, 6)
                children = na.empty((ng, 8), dtype='int64')
                for i in range(8):
                    children[:,i] = fpu.read_vector(f, "I")
                cpu_map = na.empty((ng, 8), dtype="int64")
                for i in range(8):
                    cpu_map[:,i] = fpu.read_vector(f, "I")
                rmap = na.empty((ng, 8), dtype="int64")
                for i in range(8):
                    rmap[:,i] = fpu.read_vector(f, "I")
                # We don't want duplicate grids.
                if cpu + 1 >= self.domain_id: 
                    assert(pos.shape[0] == ng)
                    oct_handler.add(cpu + 1, level, ng, pos, ind, cpu_map)
        cur = f.tell()
        f.seek(0, os.SEEK_END)
        end = f.tell()
        assert(cur == end)

    def select(self, selector):
        if id(selector) == self._last_selector_id:
            return self._last_mask
        self._last_mask = selector.fill_mask(self)
        self._last_selector_id = id(selector)
        return self._last_mask

    def count(self, selector):
        if id(selector) == self._last_selector_id:
            if self._last_mask is None: return 0
            return self._last_mask.sum()
        self.select(selector)
        return self.count(selector)

class RAMSESDomainSubset(object):
    def __init__(self, domain, indices):
        self.indices = indices
        self.domain = domain
        self.oct_handler = domain.pf.h.oct_handler

    def icoords(self, dobj):
        return self.oct_handler.icoords(self.domain.domain_id, self.indices)

    def fcoords(self, dobj):
        pass

    def fwidth(self, dobj):
        pass

    def ires(self, dobj):
        pass


class RAMSESGeometryHandler(OctreeGeometryHandler):

    def __init__(self, pf, data_style='ramses'):
        self.data_style = data_style
        self.parameter_file = weakref.proxy(pf)
        # for now, the hierarchy file is the parameter file!
        self.hierarchy_filename = self.parameter_file.parameter_filename
        self.directory = os.path.dirname(self.hierarchy_filename)

        self.float_type = na.float64
        super(RAMSESGeometryHandler, self).__init__(pf, data_style)

    def _initialize_oct_handler(self):
        self.domains = [RAMSESDomainFile(self.parameter_file, i + 1)
                        for i in range(self.parameter_file['ncpu'])]
        total_octs = sum(dom.local_oct_count for dom in self.domains)
        self.num_grids = total_octs
        self.oct_handler = RAMSESOctreeContainer(
            self.domains[0].amr_header['nx'],
            self.parameter_file.domain_left_edge,
            self.parameter_file.domain_right_edge)
        mylog.debug("Allocating %s octs", total_octs)
        self.oct_handler.allocate_domains(
            [dom.local_oct_count for dom in self.domains])
        for dom in self.domains:
            dom._read_amr(self.oct_handler)
        #assert(total_octs == self.oct_handler.nocts)
        print "TOTAL", total_octs, self.oct_handler.nocts
        print "TOTAL", total_octs / float(self.oct_handler.nocts)

    def _detect_fields(self):
        # TODO: Add additional fields
        self.field_list = [ "Density", "x-velocity", "y-velocity",
	                        "z-velocity", "Pressure", "Metallicity"]
    
    def _setup_classes(self):
        dd = self._get_data_reader_dict()
        super(RAMSESGeometryHandler, self)._setup_classes(dd)
        self.object_types.sort()

    def _count_selection(self, dobj, mask = None, oct_indices = None):
        if mask is None:
            mask = dobj.selector.select_octs(self.oct_handler)
        if oct_indices is None:
            oct_indices = self.oct_handler.count(mask, split = True) 
        domains = getattr(dobj, "_domains", None)
        if domains is None:
            count = [i.size for i in oct_indices]
            nocts = sum(count)
            domains = [RAMSESDomainSubset(dom, oct_indices[dom.domain_id - 1])
                       for dom in self.domains if count[dom.domain_id - 1] > 0]
        count = self.oct_handler.count_cells(dobj.selector, mask)
        return count

    def _identify_base_chunk(self, dobj):
        if getattr(dobj, "_chunk_info", None) is None:
            mask = dobj.selector.select_octs(self.oct_handler)
            indices = self.oct_handler.count(mask, split = True) 
            count = [i.size for i in indices]
            nocts = sum(count)
            domains = [RAMSESDomainSubset(dom, indices[dom.domain_id - 1])
                       for dom in self.domains if count[dom.domain_id - 1] > 0]
            dobj._chunk_info = domains
            dobj.size = self._count_selection(dobj, mask, indices)
            dobj.shape = (dobj.size,)
        dobj._current_chunk = list(self._chunk_all(dobj))[0]

    def _chunk_all(self, dobj):
        oobjs = getattr(dobj._current_chunk, "objs", dobj._chunk_info)
        yield YTDataChunk(dobj, "all", oobjs, dobj.size)

    def _chunk_spatial(self, dobj, ngz):
        raise NotImplementedError

    def _chunk_io(self, dobj):
        pass

class RAMSESStaticOutput(StaticOutput):
    _hierarchy_class = RAMSESGeometryHandler
    _fieldinfo_fallback = RAMSESFieldInfo
    _fieldinfo_known = KnownRAMSESFields
    
    def __init__(self, filename, data_style='ramses',
                 storage_filename = None):
        # Here we want to initiate a traceback, if the reader is not built.
        StaticOutput.__init__(self, filename, data_style)
        self.storage_filename = storage_filename

    def __repr__(self):
        return self.basename.rsplit(".", 1)[0]
        
    def _set_units(self):
        """
        Generates the conversion to various physical _units based on the parameter file
        """
        self.units = {}
        self.time_units = {}
        if len(self.parameters) == 0:
            self._parse_parameter_file()
        self._setup_nounits_units()
        self.conversion_factors = defaultdict(lambda: 1.0)
        self.time_units['1'] = 1
        self.units['1'] = 1.0
        self.units['unitary'] = 1.0 / (self.domain_right_edge - self.domain_left_edge).max()
        seconds = self.parameters['unit_t']
        self.time_units['years'] = seconds / (365*3600*24.0)
        self.time_units['days']  = seconds / (3600*24.0)
        self.time_units['Myr'] = self.time_units['years'] / 1.0e6
        self.time_units['Gyr']  = self.time_units['years'] / 1.0e9
        self.conversion_factors["Density"] = self.parameters['unit_d']
        vel_u = self.parameters['unit_l'] / self.parameters['unit_t']
        self.conversion_factors["x-velocity"] = vel_u
        self.conversion_factors["y-velocity"] = vel_u
        self.conversion_factors["z-velocity"] = vel_u

    def _setup_nounits_units(self):
        for unit in mpc_conversion.keys():
            self.units[unit] = self.parameters['unit_l'] * mpc_conversion[unit] / mpc_conversion["cm"]

    def _parse_parameter_file(self):
        # hardcoded for now
        # These should be explicitly obtained from the file, but for now that
        # will wait until a reorganization of the source tree and better
        # generalization.
        self.dimensionality = 3
        self.refine_by = 2
        self.parameters["HydroMethod"] = 'ramses'
        self.parameters["Time"] = 1. # default unit is 1...

        self.unique_identifier = \
            int(os.stat(self.parameter_filename)[stat.ST_CTIME])
        # We now execute the same logic Oliver's code does
        rheader = {}
        f = open(self.parameter_filename)
        def read_rhs(cast):
            line = f.readline()
            p, v = line.split("=")
            rheader[p.strip()] = cast(v)
        for i in range(6): read_rhs(int)
        f.readline()
        for i in range(11): read_rhs(float)
        f.readline()
        read_rhs(str)
        # Now we read the hilber indices
        self.hilbert_indices = {}
        if rheader['ordering type'] == "hilbert":
            f.readline() # header
            for n in range(rheader['ncpu']):
                dom, mi, ma = f.readline().split()
                self.hilbert_indices[int(dom)] = (float(mi), float(ma))
        self.parameters.update(rheader)
        self.current_time = self.parameters['time'] * self.parameters['unit_t']
        self.domain_right_edge = na.ones(3, dtype='float64') \
                                           * rheader['boxlen']
        self.domain_left_edge = na.zeros(3, dtype='float64')
        self.domain_dimensions = na.ones(3, dtype='int32') * 2
        # This is likely not true, but I am not sure how to otherwise
        # distinguish them.
        mylog.warning("No current mechanism of distinguishing cosmological simulations in RAMSES!")
        self.cosmological_simulation = 1
        self.current_redshift = (1.0 / rheader["aexp"]) - 1.0
        self.omega_lambda = rheader["omega_l"]
        self.omega_matter = rheader["omega_m"]
        self.hubble_constant = rheader["H0"]

    @classmethod
    def _is_valid(self, *args, **kwargs):
        if not os.path.basename(args[0]).startswith("info_"): return False
        fn = args[0].replace("info_", "amr_").replace(".txt", ".out00001")
        print fn
        return os.path.exists(fn)

