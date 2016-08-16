# coding: utf-8

from __future__ import absolute_import, division, print_function, unicode_literals

"""
This module defines a workflow for adsorption on surfaces
"""

import json
from decimal import Decimal

import numpy as np

from fireworks import FireTaskBase, Firework, FWAction, Workflow
from fireworks.utilities.fw_serializers import DATETIME_HANDLER
from fireworks.utilities.fw_utilities import explicit_serialize

from matmethods.utils.utils import env_chk, get_logger
from matmethods.vasp.drones import VaspDrone
from matmethods.vasp.database import MMDb
from matmethods.vasp.fireworks.core import OptimizeFW, TransmuterFW

from pymatgen.analysis.elasticity.elastic import ElasticTensor
from pymatgen.analysis.elasticity.strain import Deformation, IndependentStrain
from pymatgen.analysis.elasticity.stress import Stress
from pymatgen.analysis.elasticity import reverse_voigt_map
from pymatgen.io.vasp.outputs import Vasprun
from pymatgen.io.vasp.sets import MVLSlabSet
from pymatgen import Structure

__author__ = 'Joseph Montoya'
__email__ = 'montoyjh@lbl.gov'

logger = get_logger(__name__)

@explicit_serialize
class PassSlabEnergy(FireTaskBase):
    """
    Placeholder just in case I need to pass something
    """

    def run_task(self, fw_spec):
        pass
        return FWAction()


@explicit_serialize
class AnalyzeAdsorption(FireTaskBase):
    """
    Analyzes the adsorption energies in a workflow
    """

    required_params = ['structure']
    optional_params = ['db_file']

    def run_task(self, fw_spec):
        pass
        """
        # Get optimized structure
        # TODO: will this find the correct path if the workflow is rerun from the start?
        optimize_loc = fw_spec["calc_locs"][0]["path"]
        logger.info("PARSING INITIAL OPTIMIZATION DIRECTORY: {}".format(optimize_loc))
        drone = VaspDrone()
        optimize_doc = drone.assimilate(optimize_loc)
        opt_struct = Structure.from_dict(optimize_doc["calcs_reversed"][0]["output"]["structure"])
        
        d = {"analysis": {}, "deformation_tasks": fw_spec["deformation_tasks"],
             "initial_structure": self['structure'].as_dict(), 
             "optimized_structure": opt_struct.as_dict()}

        # Save analysis results in json or db
        db_file = env_chk(self.get('db_file'), fw_spec)
        if not db_file:
            with open("adsorption.json", "w") as f:
                f.write(json.dumps(d, default=DATETIME_HANDLER))
        else:
            db = MMDb.from_db_file(db_file, admin=True)
            db.collection = db.db["adsorption"]
            db.collection.insert_one(d)
            logger.info("ADSORPTION ANALYSIS COMPLETE")
        return FWAction()
        """


def get_wf_adsorption(structure, adsorption_config, vasp_input_set=None,
                      min_slab_size = 7.0, min_vacuum_size = 12.0,
                      max_normal_search = 1, center_slab = True,
                      vasp_cmd="vasp", db_file=None):
    """
    Returns a workflow to calculate elastic constants.

    Firework 1 : write vasp input set for structural relaxation,
                 run vasp,
                 pass run location,
                 database insertion.

    Firework 2 - 25: Optimize Deformed Structure
    
    Firework 26: Analyze Stress/Strain data and fit the elastic tensor

    Args:
        structure (Structure): input structure to be optimized and run
        # TODO: rethink configuration
        adsorption_config_dict (dict): configuration dictionary for adsorption
            should be a set of keys corresponding to miller indices/molecules
            e.g. {"111":Molecule("CO", [[0, 0, 0], [0, 0, 1]])}
        vasp_input_set (DictVaspInputSet): vasp input set.
        vasp_cmd (str): command to run
        db_file (str): path to file containing the database credentials.

    Returns:
        Workflow
    """

    v = vasp_input_set or MVLSlabSet(structure, bulk=True)
    fws = []
    fws.append(OptimizeFW(structure=structure, vasp_input_set=v,
                          vasp_cmd=vasp_cmd, db_file=db_file))

    max_index = max([int(i) for i in ''.join(adsorption_config.keys())])
    slabs = generate_decorated_slabs(opt_struct, max_index, min_slab_size,
                                     min_vacuum_size, max_normal_search,
                                     center_slab)
    mi_strings = [''.join([str(i) for i in slab.miller_index])
                  for slab in slabs]
    for key in adsorbate_config.keys():
        if key not in mi_strings:
            raise ValueError("Miller index not in generated slab list. "
                             "Unique slabs are {}".format(mi_strings))
    
    for slab in slabs:
        mi_string = ''.join([str(i) for i in slab.miller_index])
        if mi_string in adsorbate_config.keys():
            # Add the slab optimize firework
            trans = [SlabTransformation(slab.miller_index, min_slab_size, min_vacuum_size,
                                        shift, **slag_gen_config)]  
            fws.append(TransmuterFW(name="slab calculation",
                                    structure = structure,
                                    transformations = trans,
                                    copy_vasp_outputs=True,
                                    db_file=db_file,
                                    vasp_cmd=vasp_cmd,
                                    parents=fws[0],
                                    vasp_input_set = "MVLSlabSet",
                                    vasp_input_params = slab_input_params)
                      )
            # Generate adsorbate configurations and add fws to workflow
            asf = AdsorbateSiteFinder(slab, selective_dynamics=True)
            for molecule in adsorbate_config[mi_string]:
                structures = asf.generate_adsorption_structures(molecule)
                for struct in structures:
                    ads_sites = [site for site in slab if 
                                 site.site_properties["surface_properties"]=="adsorbate"]
                    add_adsorbate_trans = InsertSitesTransformation(
                        [site.species_string for site in ads_sites],
                        [site.frac_coords for site in ads_sites])

                    ads_trans = trans.copy() + [add_adsorbate_trans]
                    # Might need to generate adsorbate input set
                    fws.append(TransmuterFW(name="slab calculation",
                                    structure = structure,
                                    transformations = trans,
                                    copy_vasp_outputs=True,
                                    db_file=db_file,
                                    vasp_cmd=vasp_cmd,
                                    parents=fws[0],
                                    vasp_input_set = "MVLSlabSet",
                                    vasp_input_params = ads_input_params))

    wfname = "{}:{}".format(structure.composition.reduced_formula, "elastic constants")
    return Workflow(fws, name=wfname)

@explicit_serialize
class AddAdsorptionTasks(FireTaskBase):
    """
    Generates the adsorption tasks.  Note that the dynamicism
    of this workflow makes adding incar modifications and other powerups
    a little difficult.
    """
    required_params = ["adsorption_config"]
    optional_params = ["user_incar_settings", "incar_update"]

    def run_task(self):
        optimize_loc = self.spec["calc_locs"][-1]["path"]
        logger.info("PARSING INITIAL OPTIMIZATION DIRECTORY: {}".format(optimize_loc))
        drone = VaspDrone()
        optimize_doc = drone.assimilate(optimize_loc)
        opt_struct = Structure.from_dict(optimize_doc["calcs_reversed"]\
                                         [0]["output"]["structure"])

        slabs = generate_decorated_slabs(opt_struct)
        fws = []
        for slab in slabs:
            mi_string = ''.join([str(i) for i in slab.miller_index])
            if mi_string in adsorbate_config.keys():
                # Add the slab optimize firework
                vis = MVLSlabSet(slab, user_incar_settings = user_incar_settings)
                fws.append(OptimizeFW(slab, vasp_input_set = vis))
                # Generate adsorbate configurations and add fws to workflow
                asf = AdsorbateSiteFinder(slab, selective_dynamics=True)
                for molecule in adsorbate_config[mi_string]:
                    structures = asf.generate_adsorption_structures(molecule)
                    for struct in structures:
                        # Might need to generate adsorbate input set
                        fws.append(OptimizeFW(struct, vasp_input_set = vis,
                                              vasp_cmd=vasp_cmd, db_file=db_file))
                    # Analysis FW?
                    # TODO: add some info into task docs

        if incar_update:
            fws = add_modify_incar(fws, incar_update)
        return FWAction(additions = fws)

class SurfaceFW(Firework):
    def __init__(self, slab, vasp_input_set="MVLSlabSet", db_file=None,
                 parents = None, vasp_input_params=None, **kwargs):
        """
        """
        t = []

        if parents:
            t.append(CopyVaspOutputs(calc_loc))
        pass



@explicit_serialize
class WriteSlabIOSet(FireTaskBase):
    """
    #TODO, can this be made not dynamic?
    I think so, but more work
    """

    required_params = ["structure", "miller_index", "min_slab_size", 
                       "min_vacuum_size", "shift"]
    optional_params = ["vasp_input_set", "slab_gen_config"]

    def run_task(self):
        structure = self['structure'] if 'prev_calc_dir' not in self else\
                Poscar.from_file(os.path.join(self['prev_calc_dir'], 'CONTCAR')).structure
        sg = SlabGenerator(opt_structure, slab.miller_index,
                           min_slab_size, min_vacuum_size,
                           **slab_gen_config)
        new_slab = sg.get_slab(shift = slab.shift)
        if "surface_properties" in site_properties:
            adsorbate_sites = [site for site in slab.sites 
                               if site.site_properties["surface_properties"] == "adsorbate"]
            new_slab.extend(adsorbate_sites) # This could be problematic...

        vis = 
if __name__ == "__main__":
    from pymatgen.util.testing import PymatgenTest
    from pymatgen import Molecule
    co = Molecule("CO", [[0, 0, 0], [0, 0, 1.9]])
    adsorbate_config = {"111":co}
    structure = PymatgenTest.get_structure("Si")
    wf = get_wf_adsorption(structure, adsorbate_config)
