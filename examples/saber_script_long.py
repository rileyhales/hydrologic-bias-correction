import logging
from multiprocessing import Pool

import geopandas as gpd
import numpy as np
import pandas as pd

import saber

np.seterr(all="ignore")

logging.basicConfig(
    level=logging.INFO,
    filename='',
    filemode='a',
    datefmt='%Y-%m-%d %X',
    format='%(asctime)s: %(name)s - %(levelname)s - %(message)s'
)

if __name__ == "__main__":
    logger = logging.getLogger(__name__)

    # USER INPUTS - POPULATE THESE PATHS
    workdir = ''
    x_fdc_train = ''
    x_fdc_all = ''
    drain_gis = ''
    gauge_data = ''
    hindcast_zarr = ''
    # END USER INPUTS

    logger.info('Generate Clusters')
    x_fdc_train = pd.read_parquet(x_fdc_train).values
    x_fdc_all = pd.read_parquet(x_fdc_all)
    saber.cluster.generate(workdir, x=x_fdc_train)
    saber.cluster.summarize_fit(workdir)
    saber.cluster.calc_silhouette(workdir, x=x_fdc_train, n_clusters=range(2, 10))

    logger.info('Create Plots')
    saber.cluster.plot_clusters(workdir, x=x_fdc_train)
    saber.cluster.plot_centers(workdir)
    saber.cluster.plot_fit_metrics(workdir)
    saber.cluster.plot_silhouettes(workdir)

    # After this step, you should evaluate the many tables and figures that were generated by the clustering steps.
    # Determine the number of clusters which best represents the modeled discharge data.

    n_clusters = 5
    saber.cluster.predict_labels(workdir, n_clusters, x=x_fdc_all)

    # Generate assignments table
    logger.info('Generate Assignment Table')
    assign_df = saber.assign.generate(workdir)

    # Assign gauged basins
    logger.info('Assign Gauged Basins')
    assign_df = saber.assign.assign_gauged(assign_df)
    gauged_mids = assign_df[assign_df[saber.io.gid_col].notna()][saber.io.mid_col].values

    with Pool(20) as p:
        logger.info('Assign by Hydraulic Connectivity')
        df_prop_down = pd.concat(p.starmap(saber.assign.map_propagate, [(assign_df, x, 'down') for x in gauged_mids]))
        df_prop_up = pd.concat(p.starmap(saber.assign.map_propagate, [(assign_df, x, 'up') for x in gauged_mids]))
        df_prop = pd.concat([df_prop_down, df_prop_up]).reset_index(drop=True)
        df_prop = pd.concat(
            p.starmap(saber.assign.map_resolve_props, [(df_prop, x) for x in df_prop[saber.io.mid_col].unique()])
        )

        logger.info('Resolve Propagation Assignments')
        assign_df = saber.assign.assign_propagation(assign_df, df_prop)

        logger.info('Assign Remaining Basins by Cluster, Spatial, and Physical Decisions')
        for cluster_number in range(n_clusters):
            logger.info(f'Assigning basins in cluster {cluster_number}')
            # limit by cluster number
            c_df = assign_df[assign_df[saber.io.clbl_col] == cluster_number]
            # keep a list of the unassigned basins in the cluster
            mids = c_df[c_df[saber.io.reason_col] == 'unassigned'][saber.io.mid_col].values
            # filter cluster dataframe to find only gauged basins
            c_df = c_df[c_df[saber.io.gid_col].notna()]
            assign_df = pd.concat([
                pd.concat(p.starmap(saber.assign.map_assign_ungauged, [(assign_df, c_df, x) for x in mids])),
                assign_df[~assign_df[saber.io.mid_col].isin(mids)]
            ]).reset_index(drop=True)

    # Cache the completed propagation tables for inspection later
    logger.info('Caching Completed Tables')
    saber.io.write_table(df_prop, workdir, 'prop_resolved')
    saber.io.write_table(df_prop_down, workdir, 'prop_downstream')
    saber.io.write_table(df_prop_up, workdir, 'prop_upstream')
    saber.io.write_table(assign_df, workdir, 'assign_table')

    logger.info('SABER Assignment Analysis Completed')

    # Recommended Optional - Generate GIS files to visually inspect the assignments
    logger.info('Generating GIS files')
    drain_gis = gpd.read_file(drain_gis)
    saber.gis.map_by_reason(workdir, assign_df, drain_gis)
    saber.gis.map_by_cluster(workdir, assign_df, drain_gis)
    saber.gis.map_unassigned(workdir, assign_df, drain_gis)

    # Recommended Optional - Compute performance metrics
    logger.info('Compute Performance Metrics')
    saber.calibrate.mp_saber(assign_df, hindcast_zarr, gauge_data)

    # Optional - Compute the corrected simulation data
    logger.info('Computing Bias Corrected Simulations')
    with Pool(20) as p:
        p.starmap(
            saber.calibrate.map_saber,
            [[mid, asgn_mid, asgn_gid, hindcast_zarr, gauge_data] for mid, asgn_mid, asgn_gid in
             np.moveaxis(assign_df[[saber.io.mid_col, saber.io.asgn_mid_col, saber.io.gid_col]].values, 0, 0)]
        )
    logger.info('SABER Calibration Completed')

    logger.info('SABER Completed')
