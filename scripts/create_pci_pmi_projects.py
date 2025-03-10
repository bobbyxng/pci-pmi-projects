"""
Clean PCI and PMI projects from interactive PCI-PMI Transparency Platform,
based on their respective types.

https://ec.europa.eu/energy/infrastructure/transparency_platform/map-viewer/main.html
"""

import json
import logging
import re

import geopandas as gpd
import numpy as np
import pandas as pd
import pypsa
import tqdm
from dateutil import parser
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.algorithms.polylabel import polylabel
from shapely.ops import linemerge 

logging.getLogger("pyogrio._io").setLevel(logging.WARNING)  # disable pyogrio info
logger = logging.getLogger(__name__)

DISTANCE_CRS = "EPSG:3035"
MERCATOR_CRS = "EPSG:3857"  # Data is in Web Mercator projection
GEO_CRS = "EPSG:4326"  # Output is in WGS84 projection (as all other PyPSA-Eur data
BUFFER_RADIUS = 500  # meters

V_NOM_AC_DEFAULT_CONTINENTAL = 380  # kV
V_NOM_AC_DEFAULT_BALTICS = 330  # kV
V_NOM_DC_DEFAULT = 525  # kV
P_NOM_ELECTRICITY = 2000  # MW
LHV_H2 = 33.33  # kWh/kg

COLUMNS_BUSES = [
    "project_status",
    "build_year",
    "dc",
    "v_nom",
    "tags",
    "x",
    "y",
    "geometry",
]

COLUMNS_GENERATORS = [
    "project_status",
    "build_year",
    "p_nom",
    "carrier",
    "tags",
    "geometry",
]

COLUMNS_LINES = [
    "project_status",
    "length",
    "build_year",
    "underground",
    "s_nom",
    "v_nom",
    "num_parallel",
    "carrier",
    "type",
    "tags",
    "geometry",
]

COLUMNS_LINKS = [
    "project_status",
    "length",
    "build_year",
    "underground",
    "p_nom",
    "carrier",
    "tags",
    "geometry",
]

COLUMNS_STORAGE_UNITS = [
    "project_status",
    "build_year",
    "p_nom",
    "max_hours",
    "carrier",
    "tags",
    "geometry",
]

COLUMNS_STORES = [
    "project_status",
    "build_year",
    "e_nom",
    "carrier",
    "tags",
    "geometry",
]

CO2_STORE_LOCATIONS = [
    (3.9120515912952234, 52.090333354196375), # 13.1, NL
    (3.6453023968064997, 53.27954000564358), # 13.2, NL
    (5.024999999674805, 56.32600000009013), # 13.4, DK
    (12.546159999901748, 44.47808899982594), # 13.5, IT
    (3.159143747370153, 60.45166361005639), # 13.8, NO
    (18.096058679795195, 45.72414205477944), # 13.9, HR
    (11.102272000104046, 55.672252999942366), # 13.10, DK
    (24.47674514483711, 40.79165498012275), # 13.11, GR
    (-0.6565459353001007, 43.41882314187304), # 13.12, FR
    (3.44336863983537, 60.57642758996766), # 13.13, NO
]

RENAME_COLUMNS = {
    "PCI_CODE": "pci_code",
    "COMMISSIONING_DATE": "build_year",
    "CORRIDOR_CODE": "corridor",
    "PROJECT_FICHE_SHORT_TITLE": "short_title",
    "PROJECT_FULL_TITLE": "full_title",
    "TECHNICAL_DESCR": "description",
    "IMPLEMENTATION_STATUS_DESCR": "project_status",
    "COUNTRY_CONCERNED": "countries",
    "PROMOTERS": "promoters",
    "CEF_ACTION_FICHES": "project_sheet",
    "STUDIES_OR_WORKS": "studies_works",
    "OBJECTID": "object_id",
    "layerName": "layer_name",
    "geometry": "geometry",
    "geometryType": "geometry_type",
}

LAYER_MAPPING = {
    "Baltic synchronisation": "links_electricity_transmission",
    "CO2 injection and surface facilities": "stores_co2",
    "CO2 liquefaction and buffer storage": "stores_co2",
    "CO2 pipeline": "links_co2_pipeline",
    "CO2 shipping route": "links_co2_shipping",
    "Electricity line": "lines_electricity_transmission",  # contains both onshore and offshore projects, split in import; contains links and lines, split later
    "Electricity storage": "storage_units_electricity",
    "Electricity substation": "buses_electricity_transmission",  # contains both onshore and offshore projects, split in import
    "Electrolyser": "links_electrolyser",
    "Gas pipeline": "links_gas_pipeline",
    "Hydrogen pipeline": "links_h2_pipeline",
    "Hydrogen storage": "stores_h2",
    "Hydrogen terminal": "generators_h2_terminal",
    "Offshore grids": "links_offshore_grids",
    "Other essential CO2 equipement": "other",  # arbitrary assignment to mark for dropping later
    "Other hydrogen assets": "other",  # arbitrary assignment to mark for dropping later
    "Smart electricity grids": "lines_smart_electricity_transmission",
    "Smart electricity grids substation": "buses_smart_electricity_transmission",
}

COMPONENTS_MAPPING = {
    "buses_electricity_transmission": COLUMNS_BUSES,
    "buses_offshore_grids": COLUMNS_BUSES,
    "buses_smart_electricity_transmission": COLUMNS_BUSES,
    "generators_h2_terminal": COLUMNS_GENERATORS,
    "lines_electricity_transmission": COLUMNS_LINES,
    "links_electricity_transmission": COLUMNS_LINKS,
    "links_co2_pipeline": COLUMNS_LINKS,
    "links_co2_shipping": COLUMNS_LINKS,
    "links_electricity_transmission": COLUMNS_LINKS,
    "links_electrolyser": COLUMNS_LINKS,
    "links_gas_pipeline": COLUMNS_LINKS,
    "links_h2_pipeline": COLUMNS_LINKS,
    "links_offshore_grids": COLUMNS_LINKS,
    "storage_units_electricity": COLUMNS_STORAGE_UNITS,
    "stores_h2": COLUMNS_STORES,
    "stores_co2": COLUMNS_STORES,
}

UNDERGROUND_MAPPING = {  # "t" for true (underground), "f" for false (overground)
    "lines_electricity_transmission": "f",
    "links_electricity_transmission": "t",
    "links_co2_pipeline": "t",
    "links_co2_shipping": "f",
    "links_electricity_transmission": "t",
    "links_electrolyser": "f",
    "links_gas_pipeline": "t",
    "links_h2_pipeline": "t",
    "links_offshore_grids": "t",
}

CARRIER_MAPPING = {
    "generators_h2_terminal": "H2",
    "lines_electricity_transmission": "AC",
    "links_co2_pipeline": "CO2 pipeline",
    "links_co2_shipping": "CO2 pipeline",
    "links_electricity_transmission": "DC",
    "links_electrolyser": "H2",
    "links_gas_pipeline": "gas",
    "links_h2_pipeline": "H2 pipeline",
    "links_offshore_grids": "DC",
    "stores_h2": "H2 Store",
    "stores_co2": "co2 sequestered",
}

LINE_TYPES_MAPPING = {
    220: "Al/St 240/40 2-bundle 220.0",
    300: "Al/St 240/40 3-bundle 300.0",
    330: "Al/St 240/40 3-bundle 300.0",
    380: "Al/St 490/64 4-bundle 380.0",  # assuming newer cable type
    400: "Al/St 490/64 4-bundle 380.0",  # assuming newer cable type
}


def _import_projects(filepaths):
    """
    Imports project data from a list of JSON file paths and concatenates them
    into a single DataFrame.

    Parameters:
        filepaths (list): List of file paths (str) to JSON files containing project data.

    Returns:
        df_all (pd.DataFrame): A DataFrame containing consolidated project data including their original attributes, including geometry.
    """
    logger.info(f"Importing {len(filepaths)} PCI/PMI projects.")
    df_all = pd.DataFrame()
    for filepath in tqdm.tqdm(filepaths):
        with open(filepath, "r") as f:
            # Load the JSON data and append it to the list
            data = json.load(f)

        layerName = []
        attributes = []
        geometry = []
        geometryType = []

        for object in data:
            layerName.append(object["layerName"])
            attributes.append(object["attributes"])
            geometryType.append(object["geometryType"])
            geometry.append(object["geometry"])

        df = pd.DataFrame(attributes)
        df["layerName"] = layerName
        df["geometry"] = geometry
        df["geometryType"] = geometryType

        df_all = pd.concat([df_all, df], ignore_index=True)

    return df_all


def _assign_project_types(df):
    # Add value of layer mapping based on layer_name to df["project_type"]
    df["project_type"] = df["layer_name"].map(LAYER_MAPPING)

    # Assign project types based on layerName
    pci_offshore_grids = df[df["layer_name"] == "Offshore grids"]["pci_code"].unique()
    bool_buses_electricity_transmission = df["layer_name"] == "Electricity substation"
    bool_lines_electricity_transmission = df["layer_name"] == "Electricity line"
    bool_offshore_grids = df["pci_code"].isin(pci_offshore_grids)
    bool_is_point = df["geometry_type"] == "esriGeometryPoint"

    # Assign project types based on boolean conditions
    df.loc[
        bool_buses_electricity_transmission & ~bool_offshore_grids, "project_type"
    ] = LAYER_MAPPING["Electricity substation"]
    df.loc[
        bool_lines_electricity_transmission & ~bool_offshore_grids, "project_type"
    ] = LAYER_MAPPING["Electricity line"]

    # Assign offshore grids project types
    bool_buses_offshore_grids = (
        bool_buses_electricity_transmission & bool_offshore_grids
    ) | (bool_is_point & bool_offshore_grids)
    bool_lines_offshore_grids = bool_offshore_grids & ~bool_is_point

    df.loc[bool_buses_offshore_grids, "project_type"] = "buses_offshore_grids"
    df.loc[bool_lines_offshore_grids, "project_type"] = "links_offshore_grids"

    # Correct project types for lines that are actually DC links
    df.loc[
        (df["project_type"] == "lines_electricity_transmission")
        & (
            df["description"].str.lower().str.contains("dc")
            | df["description"].str.lower().str.contains("converter")
            | df["description"].str.lower().str.contains("vsc")
        ),
        "project_type",
    ] = "links_electricity_transmission"

    # Remove rows with layer_name containing other
    df = df[~df["layer_name"].str.lower().str.contains("other")]

    return df


def _create_geometries(row):
    """
    Creates geometries based on the type specified in the input row.

    Parameters:
        row (pd.Series): A pandas Series containing geometry information with the following keys:
            - 'geometry_type' (str): The type of geometry ('esriGeometryPolyline', 'esriGeometryPoint', or 'esriGeometryPolygon').
            - 'geometry' (dict): A dictionary containing the geometry data:
                - For 'esriGeometryPolyline': Contains 'paths', a list of coordinate lists.
                - For 'esriGeometryPoint': Contains 'x' and 'y' coordinates.
                - For 'esriGeometryPolygon': Contains 'rings', a list of coordinate lists.

    Returns:
        shapely.geometry.base.BaseGeometry: A Shapely geometry object (MultiLineString, Point, or Polygon) based on the input geometry type.
    """
    # Handle esriGeometryPolyline (LineString)
    if row["geometry_type"] == "esriGeometryPolyline":
        lines = [LineString(path) for path in row["geometry"]["paths"]]
        row_geom = linemerge(lines)

    # Handle esriGeometryPoint (Point)
    elif row["geometry_type"] == "esriGeometryPoint":
        point = Point(row["geometry"]["x"], row["geometry"]["y"])
        row_geom = point

    # Handle esriGeometryPolygon (Polygon)
    elif row["geometry_type"] == "esriGeometryPolygon":
        for ring in row["geometry"]["rings"]:
            row_geom = Polygon(ring)

    return row_geom


def _split_multilinestring(row):
    """
    Splits rows containing a MultiLineString geometry into multiple rows,
    converting them to a single LineString. New rows inherit all other
    attributes from the original row. Non-MultiLineString rows are returned as-
    is.

    Parameters:
        row (pd.Series): A pandas Series containing a 'geometry' column with a MultiLineString or LineString.

    Returns:
        row (pd.Series): A row containing a LineString geometry including their original attributes.
    """
    geom = row["geometry"]
    if isinstance(geom, MultiLineString):
        # Convert MultiLineString into a list of LineStrings
        lines = [line for line in geom.geoms]
        # Create a DataFrame with the new rows, including all other columns
        return pd.DataFrame(
            {
                "geometry": lines,
                **{
                    col: [row[col]] * len(lines)
                    for col in row.index
                    if col != "geometry"
                },
            }
        )
    else:
        # Return the original row as a DataFrame, including all columns
        return pd.DataFrame([row])


def _remove_redundant_components(df):
    """
    Remove redundant components, such as entries with 'Polygon' geometries or
    entries that are already commissioned.

    Parameters:
        df (pd.DataFrame): The input DataFrame.

    Returns:
        df (pd.DataFrame): The cleaned DataFrame with redundant components removed.
    """

    df = df[df["geometry"].apply(lambda x: x.geom_type != "Polygon")]
    df = df[df["project_status"] != "commissioned"]

    return df


def _clean_build_year(row, build_year_fallback=2030):
    """
    Cleans and standardises the 'build_year' field in a given row of data.

    This function handles various cases for the 'build_year' field:
    - If 'build_year' is a string, it attempts to parse it as a date.
      - Optional: If the month is December, it returns the next year (deactivated).
      - If parsing fails, it checks if the string is a 4-digit year.
      - If the string is 'Null', it uses a manual correction based on 'pci_code' or a fallback year.
    - If 'build_year' is an integer or float, it returns it as an integer.
    - If all parsing attempts fail, it returns build_year_fallback.

    Parameters:
        row (pd.Series): A row containing 'build_year' and 'pci_code' columns.

    Returns:
        year (int): A single year as an integer.
    """
    # Manual corrections: https://acer.europa.eu/sites/default/files/documents/Publications_annex/2023_ACER_PCI_Report-AnnexI_Electricity.pdf
    # PCI code
    build_year_manual = {
        "1.7.1": 2030,
        "1.7.2": 2030,
    }

    # Handle case where 'build_year' is a string
    if isinstance(row["build_year"], str):
        try:
            # Try to parse the date
            parsed_date = parser.parse(row["build_year"], dayfirst=True, fuzzy=True)
            year = parsed_date.year

            # If the month is December, return next year
            # if parsed_date.month == 12:
            #     return year + 1
            return year
        except (ValueError, parser.ParserError):
            # Handle cases where parsing fails or format is unexpected
            if row["build_year"].isdigit() and len(row["build_year"]) == 4:
                return int(row["build_year"])
            if row["build_year"] == "Null":
                if row["pci_code"] in build_year_manual:
                    return build_year_manual[row["pci_code"]]
                return build_year_fallback

    # Handle case where 'build_year' is an integer or float
    try:
        return int(row["build_year"])
    except (ValueError, TypeError):
        return build_year_fallback


def _clean_status(row):
    """
    Standardises the 'project_status' field in a given row of data.

    Parameters:
        row (pd.Series): A row containing 'project_status' column.

    Returns:
        row (pd.Series): A row with the 'project_status' field standardised.
    """
    status_mapping = dict(
        {
            "Under consideration": "under_consideration",
            "Under consideration ": "under_consideration",
            "Planned but not yet in permitting": "in_planning",
            "Permitting": "in_permitting",
            "Under construction": "under_construction",
            "Commissioned": "commissioned",
        }
    )

    if row["project_status"] in status_mapping:
        return status_mapping[row["project_status"]]

    return row["project_status"]


def _clean_columns(df):
    """
    Renames and reduces the columns in the DataFrame to only include the
    necessary columns. Further cleaning is applied to the 'build_year' (year)
    and 'project_status' (standardised) columns.

    Parameters:
        df (pd.DataFrame): The input DataFrame containing the original columns.

    Returns:
        df (pd.DataFrame): The cleaned DataFrame with renamed and reduced columns.
    """
    df = df[RENAME_COLUMNS.keys()].rename(columns=RENAME_COLUMNS)  # Rename columns
    df["build_year"] = df.apply(_clean_build_year, axis=1)  # Clean build_year column
    df["project_status"] = df.apply(_clean_status, axis=1)  # Clean status column

    return df


def _create_unique_ids(df):
    """
    Create unique IDs for each project, starting with the PCI code and adding a
    two-digit numerical suffix "-01", "-02", etc. only if there are multiple
    geometries for the same project.

    Parameters:
        df (pd.DataFrame): The input DataFrame containing the 'pci_code' column.

    Returns:
        df (pd.DataFrame): An indexed DataFrame with unique IDs for each project.
    """
    # Count the occurrences of each 'pci_code'
    pci_code_counts = df["pci_code"].value_counts()

    # Generate cumulative counts within each 'pci_code' group
    df["count"] = df.groupby("pci_code").cumcount() + 1  # Start counting from 1, not 0

    # Add a two-digit suffix if the 'pci_code' appears more than once
    df["suffix"] = df.apply(
        lambda row: (
            f"-{str(row['count']).zfill(2)}"
            if pci_code_counts[row["pci_code"]] > 1
            else ""
        ),
        axis=1,
    )

    # Create the 'id' column by combining 'pci_code' and suffix
    df["id"] = "PCI-" + df["pci_code"] + df["suffix"]

    # Clean up by dropping the helper columns
    df = df.drop(columns=["count", "suffix"])

    df.set_index("id", inplace=True)

    return df


def _columns_to_tags(
    df,
    columns=[
        "pci_code",
        "corridor",
        "short_title",
        "full_title",
        "description",
        "countries",
        "promoters",
        "project_sheet",
        "studies_works",
        "object_id",
        "layer_name",
        "geometry_type",
    ],
):
    """
    Converts specified columns to a 'tags' column containing a dictionary of
    tags.

    Parameters:
        df (pd.DataFrame): The input DataFrame containing the specified columns.
        columns (list): A list of column names to be converted to tags.

    Returns:
        df (pd.DataFrame): The DataFrame with the specified columns removed and a new 'tags' column containing a dictionary of tags
    """
    df["tags"] = df[columns].apply(lambda x: {k: v for k, v in zip(columns, x)}, axis=1)
    df = df.drop(columns=columns)

    return df


def _create_components_dict(projects, project_types):
    """
    Create a dictionary of GeoDataFrames/components for specified project
    types.

    This function filters the input DataFrame based on the project types and
    maps the filtered data to a new DataFrame with columns specified in the
    COMPONENTS_MAPPING. The resulting DataFrame is then converted to a
    GeoDataFrame and reprojected to the desired coordinate reference system.

    Parameters:
        projects (pd.DataFrame): The input DataFrame containing project data.
        project_types (list): A list of project types to filter and process.

    Returns:
        components (dict): A dictionary where keys are project types and values are
              GeoDataFrames containing the filtered and processed project data.
    """
    logger.info("")
    logger.info("Creating components.")
    components = {}
    subset = sorted(
        set.intersection(set(project_types), set(COMPONENTS_MAPPING.keys()))
    )
    for component in subset:
        # Filter the projects DataFrame based on project_type corresponding to the component
        filtered_projects = projects[projects["project_type"] == component]
        columns = COMPONENTS_MAPPING[component]

        # Create an empty DataFrame with the respective columns and the filtered index
        df = pd.DataFrame(index=filtered_projects.index, columns=columns)
        filtered_projects = filtered_projects.reindex(df.index)  # ensure alignment

        # Assign values from filtered_projects where the columns exist in both DataFrames
        for col in df.columns:
            if col in filtered_projects.columns:
                # Assign values using .loc to ensure correct index alignment
                df[col] = filtered_projects.loc[df.index, col]

        df = gpd.GeoDataFrame(df, crs=MERCATOR_CRS, geometry="geometry").to_crs(
            crs=GEO_CRS
        )  # Convert to GeoDataFrame and reproject
        df_meters = df[["geometry"]].to_crs(DISTANCE_CRS)

        if "length" in df.columns:
            df["length"] = df_meters.length.div(1e3).round(1)  # Calculate length in km

        # Initiate 'underground' column based on type
        if component in UNDERGROUND_MAPPING and "underground" in df.columns:
            df["underground"] = UNDERGROUND_MAPPING[component]

        # Store the DataFrame in the dictionary
        components[component] = df

        # Initiate 'carrier' column based on type
        if component in CARRIER_MAPPING and "carrier" in df.columns:
            df["carrier"] = CARRIER_MAPPING[component]

    return components


def _set_electrical_params(components):
    logger.info("Setting electrical parameters.")
    dc_projects = list(
        set(
            components["links_electricity_transmission"]["tags"].apply(
                lambda x: x["pci_code"]
            )
        )
        | (
            set(
                components["links_offshore_grids"]["tags"].apply(
                    lambda x: x["pci_code"]
                )
            )
        )
    )

    logger.info(" - Setting bus types")
    for component, df in components.items():
        # Determine bus types (AC/DC)
        if "buses" in component:
            # if "description" contains DC or converter
            dc_bus_indicator = [
                "dc",
                "converter",
                "vsc",
                "multiterminal",
                "bipolar",
                "hub",
            ]
            df["dc"] = df["tags"].apply(
                lambda row: any(
                    [dc in row["description"].lower() for dc in dc_bus_indicator]
                )
            )
            df["dc"] = df["dc"].apply(lambda x: "t" if x else "f")
            # Overwrite bus type if df["tags"]["pci_code"] is in dc projects
            df["dc"] = df.apply(
                lambda x: "t" if x["tags"]["pci_code"] in dc_projects else x["dc"],
                axis=1,
            )

    logger.info(" - Extracting and setting voltage levels")
    for component, df in components.items():
        # Extract voltage levels
        if "v_nom" in df.columns:
            df["v_nom"] = df["tags"].apply(
                lambda x: (
                    x["description"].split("kV")[0].split()[-1]
                    if "kV" in x["description"]
                    else None
                )
            )
            # EXTRACT ONLY NUMERIC STRING
            df["v_nom"] = df["v_nom"].apply(
                lambda x: x if x is None else "".join(filter(str.isdigit, x))[0:3]
            )

            # Set default fallback values if v_nom is None
            if "dc" in df.columns:
                df.loc[df["v_nom"].isna() & (df["dc"] == "t"), "v_nom"] = (
                    V_NOM_DC_DEFAULT
                )

                baltics_indicator = ["estonia", "latvia", "lithuania", "bemip"]
                bool_baltics = df["tags"].apply(
                    lambda x: any(
                        [
                            country in x["description"].lower()
                            for country in baltics_indicator
                        ]
                    )
                ) | (
                    df["tags"].apply(
                        lambda x: any(
                            [
                                corridor in x["corridor"].lower()
                                for corridor in baltics_indicator
                            ]
                        )
                    )
                )
                df.loc[
                    df["v_nom"].isna() & (df["dc"] == "f") & bool_baltics, "v_nom"
                ] = V_NOM_AC_DEFAULT_BALTICS

                # Last fallback, remaining AC to continental default
                df.loc[df["v_nom"].isna() & (df["dc"] == "f"), "v_nom"] = (
                    V_NOM_AC_DEFAULT_CONTINENTAL
                )

            df.loc[df["v_nom"].isna(), "v_nom"] = V_NOM_AC_DEFAULT_CONTINENTAL
            df["v_nom"] = df["v_nom"].astype(int)

    return components


def _extract_params(text, units):
    """
    Extracts all numerical values followed by a unit from the given text.

    Parameters:
        text (str): The text to search for the patterns.
        units (list of str): A list of units to match after the numerical value.

    Returns:
        list of str: A list of matched strings containing the numbers and units.
    """
    # Create a regex pattern to match a number (with optional decimal: . or ,)
    # followed by any unit in the list, allowing for an optional space before the unit
    units_pattern = r"(?:{})\b".format(
        "|".join(units)
    )  # Word boundary only on the right side

    # The main pattern includes optional spaces before the units
    pattern = r"[\d]+[.,]?[\d]*\s*{}|[\d]+[.,]?[\d]*{}".format(
        units_pattern, units_pattern
    )

    # Find all matches of the pattern in the given text
    matches = re.findall(pattern, text)

    # Replace ',' with '.' for each match and return the list of results
    return [match.replace(",", ".") for match in matches if match]


def _set_p_nom_elec(value):
    """
    Sets the nominal power for electricity based on the provided value.

    Parameters:
        value (str or None): The input value representing the nominal power.
            It can be a string containing a number with units
            (e.g., 'GW', 'MW', 'MVA') or None.

    Returns:
        float: The nominal power in MW. If the input value is in GW, it is
            converted to MW by multiplying by 1000. If the input value is
            None or an empty string, a default value (P_NOM_ELECTRICITY)
            is returned.
    """
    if value is None:
        return P_NOM_ELECTRICITY
    else:
        # Check if the value contains 'GW'
        if "GW" in value:
            # Multiply by 1000 and drop the unit
            return float(value.replace("GW", "").strip()) * 1000
        else:
            # Drop spaces and any units (MW, MVA)
            cleaned_value = (
                value.replace("MW", "").replace("MVA", "").replace(" ", "").strip()
            )
            return float(cleaned_value) if cleaned_value else P_NOM_ELECTRICITY


def _set_params_links_electricity(df):
    """
    Sets the nominal power (p_nom) for electricity transmission and offshore
    grid links based on tags and manual corrections.

    Parameters:
        df (pd.DataFrame): DataFrame containing links data.

    Returns:
        df (pd.DataFrame): Updated DataFrame with nominal power (p_nom) set for each link.
        The function performs the following steps:
        1. Extracts the relevant components for electricity transmission and offshore grids.
        2. Sets the nominal power (p_nom) based on the tags' descriptions.
        3. Applies manual corrections for specific projects.
    """
    df["p_nom"] = df["tags"].apply(
        lambda x: max(
            _extract_params(x["description"], ["MW", "MVA", "GW"]), default=None
        )
    )
    df["p_nom"] = df["p_nom"].apply(_convert_to_mw)
    df["p_nom"] = df["p_nom"].fillna(P_NOM_ELECTRICITY)

    # Manual corrections
    # NSWPH, index containing "4.1", set p_nom to 2000
    df.loc[df["tags"].apply(lambda x: "4.1" in x["pci_code"]), "p_nom"] = 2000

    # Bornholm Energy Island, Longer line to DK, p_nom is 1200 MW, shorter line to DE, p_nom is 2000 MW
    df.loc[
        (df["tags"].apply(lambda x: "5.2" in x["pci_code"])) & (df["length"] < 150),
        "p_nom",
    ] = 2000
    df.loc[
        (df["tags"].apply(lambda x: "5.2" in x["pci_code"])) & (df["length"] > 150),
        "p_nom",
    ] = 1200

    return df


def _set_params_lines_electricity(df):
    """
    Sets the nominal power (s_nom) for electricity transmission lines based on
    tags and manual corrections.

    Parameters:
        df (pd.DataFrame): DataFrame containing links data.

    Returns:
        df (pd.DataFrame): Updated DataFrame with nominal power (s_nom) set for each link.
        The function performs the following steps:
        1. Extracts the relevant components for electricity transmission and offshore grids.
        2. Sets the nominal power (s_nom) based on the tags' descriptions.
        3. Applies manual corrections for specific projects.
    """
    df["s_nom"] = df["tags"].apply(
        lambda x: max(
            _extract_params(x["description"], ["MW", "MVA", "GW"]), default=None
        )
    )
    df["s_nom"] = df["s_nom"].apply(_convert_to_mw)

    circuit_mapping = {
        "single circuit": 1,
        "single-circuit": 1,
        "1 circuit": 1,
        "1-circuit": 1,
        "double circuit": 2,
        "double-circuit": 2,
        "2 circuit": 2,
        "2-circuit": 2,
    }

    # Determine number of parallel circuits
    df["num_parallel"] = df["tags"].apply(
        lambda x: next(
            (
                circuit_mapping[circuit]
                for circuit in circuit_mapping
                if circuit in x["description"].lower()
            ),
            None,
        )
    )
    df.loc[df["num_parallel"].isna(), "num_parallel"] = (
        1  # Set default value to 1 if not found
    )

    # Set line types based on voltage levels
    df["type"] = df["v_nom"].apply(lambda x: LINE_TYPES_MAPPING.get(x, None))

    # Set default s_nom for missing values
    df.loc[df["s_nom"].isna(), "s_nom"] = (
        np.sqrt(3)
        * df.loc[df["s_nom"].isna(), "type"].map(pypsa.Network().line_types["i_nom"])
        * df.loc[df["s_nom"].isna(), "v_nom"]
        * df.loc[df["s_nom"].isna(), "num_parallel"]
    ).round(0)

    return df


def _mtpy_to_mw(mt_per_year: float) -> float:
    """
    Convert million tonnes per year (Mt/y) of hydrogen to megawatts (MW).

    Parameters:
        mt_per_year (float): The amount of hydrogen in million tonnes per year (Mt/y).

    Returns:
        float: The equivalent power in megawatts (MW).
    """
    kg_per_year = mt_per_year * 1e9  # kg/y
    energy_kwh_per_year = kg_per_year * LHV_H2  # kWh/y
    energy_mwh_per_year = energy_kwh_per_year / 1e3  # MWh/y
    capacity_mw = energy_mwh_per_year / 8760  # MW

    return capacity_mw


def _gwhpd_to_mw(gwh_per_day: float) -> float:
    """
    Convert gigawatt-hours per day (GWh/d) to megawatts (MW).

    Parameters:
        gwh_per_day (float): The amount of energy in gigawatt-hours per day (GWh/d).

    Returns:
        float: The equivalent power in megawatts (MW).
    """
    capacity_mw = gwh_per_day * 1e3 / 24  # MW

    return capacity_mw


def _convert_to_mw(value: str) -> float:
    """
    Converts a given string including units to MW and returns a float.

    Parameters:
        value (str): The input value representing the nominal power.
            It can be a string containing a number with units
            (e.g., 'GWh/d', 'Mt/y', 'GW', 'MW') or None.

    Returns
        float: The equivalent value in MW
    """
    if value is None:
        return None
    else:
        if "MVA" in value:
            return float(value.replace("MVA", "").strip())
        if "GWh/day" in value:
            return _gwhpd_to_mw(float(value.replace("GWh/day", "").strip()))
        if "GWh/d" in value:
            return _gwhpd_to_mw(float(value.replace("GWh/d", "").strip()))
        if "Mt/y" in value:
            # Convert million tonnes per year (Mt/y) of hydrogen to megawatts (MW)
            return _mtpy_to_mw(float(value.replace("Mt/y", "").strip()))
        if "MW" in value:
            # Remove 'MW' and convert to float
            return float(value.replace("MW", "").strip())
        if "GW" in value:  # needs to be checked last, otherwise issues with GWh/d occur
            # Multiply by 1000 to convert to MW
            return float(value.replace("GW", "").strip()) * 1000
        else:
            return None


def _convert_array_to_mw(array: np.ndarray) -> np.ndarray:
    """
    Converts an array of strings including units to MW and returns a float
    array.

    Parameters:
        array (np.ndarray): The input array containing values representing the nominal power.
            Each value can be a string containing a number with units
            (e.g., 'GWh/d', 'Mt/y', 'GW', 'MW') or None.

    Returns:
        np.ndarray: The equivalent array in MW.
    """
    if len(array) == 0:
        return np.array([])
    return np.array([_convert_to_mw(value) for value in array])


def _set_params_links_h2(df):
    """
    Sets the nominal power (p_nom) for hydrogen pipelines.

    Parameters:
        df (pd.DataFrame): DataFrame containing links data.

    Returns:
        df (pd.DataFrame): Updated DataFrame with nominal power (p_nom) set for each link.
        The function performs the following steps:
        1. Extracts the relevant components for hydrogen pipelines.
        2. Sets the nominal power (p_nom) based on the tags' descriptions and converts units like GWh/d and Mt/y to equivalent MW.
        3. Applies manual corrections for specific projects.
    """
    # Extract relevant values first, if GWh/d exists, use this value first (provides most accurate p_nom)
    df["p_nom"] = df["tags"].apply(
        lambda x: _extract_params(x["description"], ["GWh/d", "GWh/day"])
    )

    # Try again with Mt/y if p_nom list is empty
    no_p_nom = df["p_nom"].apply(lambda x: len(x) == 0)
    df.loc[no_p_nom, "p_nom"] = df.loc[no_p_nom, "tags"].apply(
        lambda x: _extract_params(x["description"], ["Mt/y"])
    )

    # Try again with MW and GW if p_nom list is still empty
    no_p_nom = df["p_nom"].apply(lambda x: len(x) == 0)
    df.loc[no_p_nom, "p_nom"] = df.loc[no_p_nom, "tags"].apply(
        lambda x: _extract_params(x["description"], ["MW", "GW"])
    )

    # Convert extracted values to equivalent MW
    df["p_nom"] = df["p_nom"].apply(_convert_array_to_mw)
    # Keep the maximum of all extracted values
    df["p_nom"] = df["p_nom"].apply(lambda x: max(x, default=None))

    # Manual corrections:
    # PCI 10.4, source: https://ehb.eu/page/european-hydrogen-backbone-maps
    df.loc[df["tags"].apply(lambda x: "10.4" in x["pci_code"]), "p_nom"] = _gwhpd_to_mw(
        144
    )

    # Round p_nom
    df["p_nom"] = df["p_nom"].round(0)

    return df


def _create_endpoints(gdf):
    """
    Creates a GeoDataFrame containing the endpoints of the input GeoDataFrame.

    Parameters:
        - gdf (GeoDataFrame): The input GeoDataFrame containing the projects.

    Returns:
        - points (GeoDataFrame): The output GeoDataFrame containing the endpoints of the input geometries.
    """

    points0 = gdf["geometry"].apply(
        lambda x: (
            x.boundary.geoms[0]
            if hasattr(x.boundary, "geoms") and len(x.boundary.geoms) > 0
            else None
        )
    )

    points1 = gdf["geometry"].apply(
        lambda x: (
            x.boundary.geoms[-1]
            if hasattr(x.boundary, "geoms") and len(x.boundary.geoms) > 1
            else None
        )
    )

    points = pd.concat([points0, points1], axis=0)
    # Create a GeoDataFrame with the points
    points = gpd.GeoDataFrame(points, columns=["geometry"], crs=gdf.crs).reset_index(
        drop=True
    )
    # Drop by duplicates in geometry column
    points.drop_duplicates(subset=["geometry"], inplace=True)
    # Drop nas
    points.dropna(subset=["geometry"], inplace=True)
    points.reset_index(drop=True, inplace=True)

    return points


def _split_to_segments(
    gdf, buffer_radius=BUFFER_RADIUS, distance_crs=DISTANCE_CRS, geo_crs=GEO_CRS
):
    """
    Split projects into their individual subcomponents based on junction points (at a tolerance of buffer_radius).
    buffer_radius defaults to 500 meters

    Parameters:
        - gdf (GeoDataFrame): The input GeoDataFrame containing the projects.
        - buffer_radius (float): The buffer radius to use for the junction points.
        - distance_crs (str): The coordinate reference system to use for distance calculations.
        - geo_crs (str): The coordinate reference system to use for the output GeoDataFrame.

    Returns:
        - gdf_split (GeoDataFrame): The output GeoDataFrame containing the split projects in geo_crs projection.
    """
    logger.info("Splitting linestrings at junction points into segments.")
    gdf_split = gdf.copy().to_crs(distance_crs)
    gdf_subcomponents = gpd.GeoDataFrame(
        geometry=list(gdf_split["geometry"].union_all().geoms), crs=gdf_split.crs
    )

    points = _create_endpoints(gdf_subcomponents)
    points.to_crs(distance_crs, inplace=True)

    points["buffer"] = points["geometry"].buffer(buffer_radius)

    # Split linestrings of gdf by union of points[buffer]
    gdf_split["geometry"] = gdf_split["geometry"].apply(
        lambda x: x.difference(points["buffer"].union_all())
    )

    # Drop empty geometries
    gdf_split = gdf_split[~gdf_split["geometry"].is_empty]

    gdf_split.reset_index(inplace=True)

    # All rows with multilinestrings, split them into their individual linestrings and fill the rows with the same data
    gdf_split = pd.concat(
        gdf_split.apply(_split_multilinestring, axis=1).tolist(), ignore_index=True
    )

    gdf_split = gpd.GeoDataFrame(gdf_split, crs=distance_crs).to_crs(geo_crs)
    gdf_split.set_index("id", inplace=True)

    return gdf_split


def _clip_to_offshore(gdf, regions, distance_crs=DISTANCE_CRS, geo_crs=GEO_CRS):
    buffer = 10000  # m
    gdf_clip = gdf.copy().to_crs(DISTANCE_CRS)
    regions_union = regions["geometry"].to_crs(DISTANCE_CRS).union_all()
    regions_union_buffer = regions_union.buffer(buffer)

    gdf_clip["geometry"] = gdf_clip["geometry"].apply(
        lambda x: x.difference(regions_union)
    )
    gdf_clip = gdf_clip[~gdf_clip["geometry"].is_empty]

    # All rows with multilinestrings, split them into their individual linestrings and fill the rows with the same data
    gdf_clip.reset_index(inplace=True)
    gdf_clip = pd.concat(
        gdf_clip.apply(_split_multilinestring, axis=1).tolist(), ignore_index=True
    )
    gdf_clip = gpd.GeoDataFrame(gdf_clip, crs=distance_crs)

    # Drop those geometries that are fully within the buffer of regions_union
    gdf_clip = gdf_clip[
        ~gdf_clip["geometry"].apply(lambda x: x.within(regions_union_buffer))
    ]

    gdf_clip.set_index("id", inplace=True)
    gdf_clip = gdf_clip.to_crs(geo_crs)

    return gdf_clip


def _map_params_to_projects(df, params):
    df.reset_index(inplace=True)
    df["pci_code"] = df["tags"].apply(lambda x: x["pci_code"])

    if "p_nom" in df.columns:
        df = df.drop(columns=["p_nom"])

    # Left join
    df = df.merge(params, how="inner", on="pci_code")

    # Add additional columns to tags dict
    columns = ["pci_code", "source", "comments"]
    # for column in columns:
    #     # Add column "source" to dictionary in tags column
    #     df["tags"] = df.apply(
    #         lambda x: {**x["tags"], column: x[column] if column not in {**x["tags"]} else None},
    #         axis=1,
    #     )

    df = df.drop(columns=columns)

    df.set_index("id", inplace=True)

    return df


def _create_link_ends(df):

    buses = pd.concat(
        [
            df["geometry"].apply(lambda x: Point(x.coords[0])),
            df["geometry"].apply(lambda x: Point(x.coords[-1])),
        ]
    ).reset_index(drop=True)

    return buses


def _create_stations(buses, tol=500):
    buses = buses.to_crs(DISTANCE_CRS).buffer(tol).to_crs(GEO_CRS)

    gdf = gpd.GeoDataFrame(geometry=[poly for poly in buses.union_all().geoms], crs=GEO_CRS)
    
    # Create PoI
    gdf["poi"] = (
        gdf["geometry"]
        .to_crs(DISTANCE_CRS)
        .apply(lambda x: polylabel(x, tolerance=tol / 2))
        .to_crs(GEO_CRS)
    )

    return gdf


def _drop_internal_links(links, buses):

    links = links.copy().reset_index()
    internal_links = gpd.sjoin(links, buses, how="inner", predicate="within").index

    links.drop(internal_links, inplace=True)
    links.set_index("id", inplace=True)

    return links


def _extend_links_to_buses(links, buses):
    links = links.copy().reset_index()
    
    bus0 = gpd.GeoDataFrame(links["geometry"].apply(lambda x: Point(x.coords[0])), geometry="geometry", crs=links.crs)
    bus1 = gpd.GeoDataFrame(links["geometry"].apply(lambda x: Point(x.coords[-1])), geometry="geometry", crs=links.crs)

    links["bus0"] = bus0
    links["bus0_ext"] = gpd.sjoin(bus0, buses, how="left", predicate="within")["poi"]
    links["bus0_line"] = links.apply(
        lambda row: LineString([row["bus0"], row["bus0_ext"]]), axis=1
    )
    links["bus1"] = bus1
    links["bus1_ext"] = gpd.sjoin(bus1, buses, how="left", predicate="within")["poi"]
    links["bus1_line"] = links.apply(
        lambda row: LineString([row["bus1"], row["bus1_ext"]]), axis=1
    )
    links["geometry"] = links.apply(
        lambda row: linemerge([row["bus0_line"], row["geometry"], row["bus1_line"]]), axis=1
    )

    links = links.drop(columns=["bus0", "bus0_ext", "bus0_line", "bus1", "bus1_ext", "bus1_line"])

    return links.set_index("id")


def _calculate_length(gdf):
    gdf["length"] = gdf.to_crs(DISTANCE_CRS).length.div(1e3).round(1)  # Calculate length in km

    return gdf


if __name__ == "__main__":
    if "snakemake" not in globals():
        from _helpers import mock_snakemake

        snakemake = mock_snakemake(
            "create_pci_pmi_projects",
        )

    exclude = snakemake.params.exclude
    json_files = snakemake.input.raw
    corrections = snakemake.input.corrections

    # Read params for storage units
    params_stores_co2 = pd.read_csv(snakemake.input.params_stores_co2, dtype={"pci_code": str})
    params_stores_h2 = pd.read_csv(snakemake.input.params_stores_h2, dtype={"pci_code": str})
    params_storage_units_electricity = pd.read_csv(snakemake.input.params_storage_units_electricity, dtype={"pci_code": str})

    # Overwrite parsed data
    overwrite_parsed_data = snakemake.params.overwrite_parsed_data
    if overwrite_parsed_data["links_h2_pipeline"]:
        params_links_h2_pipeline = pd.read_csv(snakemake.input.params_links_h2_pipeline, dtype={"pci_code": str})
        params_links_h2_pipeline["p_nom"] = (
            params_links_h2_pipeline["p_Mtpa"] / params_links_h2_pipeline["utilisation_factor"] 
            * 1e6 * LHV_H2 / 8760
        ).round(0)

    if overwrite_parsed_data["links_co2_pipeline"]:
        params_links_co2_pipeline = pd.read_csv(snakemake.input.params_links_co2_pipeline, dtype={"pci_code": str})

        params_links_co2_pipeline["p_nom"] = (
            params_links_co2_pipeline["p_Mtpa"] / params_links_co2_pipeline["utilisation_factor"] 
            * 1e6 / 8760
        ).round(0)

    # INITIALISATION OF PROJECTS
    projects = _import_projects(json_files)  # Import projects from JSON files
    projects_corrections = _import_projects(corrections)  # Import corrections from CSV files
    projects_corrections.PCI_CODE.unique()
    # Overwrite projects with corrections, if the pci_code is in both
    projects = projects[~projects["PCI_CODE"].isin(projects_corrections["PCI_CODE"].unique())]
    projects = pd.concat([projects, projects_corrections], ignore_index=True)

    projects = _clean_columns(projects)
    projects = _assign_project_types(
        projects
    )  # Assign project types based on layerName

    # Remove projects with excluded PCI codes
    logger.info(f"Excluding {len(exclude)} projects:")
    logger.info(f"{exclude}")
    projects = projects[~projects["pci_code"].isin(exclude)]

    # Storage units CO2
    list_co2_sequestration = projects[
        projects.layer_name == "CO2 injection and surface facilities"
    ]["pci_code"].unique()

    projects = projects[
        ~(
            (projects.layer_name == "CO2 liquefaction and buffer storage")
            & (projects.pci_code.isin(list_co2_sequestration))
        )
    ]
    projects = projects[
        ~(
            (projects.layer_name == "CO2 liquefaction and buffer storage")
            & (~projects.pci_code.isin(params_stores_co2["pci_code"]))
        )
    ]

    # FIXING GEOMETRIES
    projects["geometry"] = projects.apply(
        _create_geometries, axis=1
    )  # Create Points, LineStrings, and Polygons
    projects = _remove_redundant_components(
        projects
    )  # Remove redundant components such as 'Polygon' geometries or already commissioned projects

    projects = pd.concat(
        projects.apply(_split_multilinestring, axis=1).tolist(), ignore_index=True
    )  # Split MultiLineStrings into LineStrings

    # FURTHER PROCESSING
    projects = _create_unique_ids(
        projects
    )  # Create unique IDs, dataframe is indexed by ID

    projects = _columns_to_tags(
        projects
    )  # Move columns with additional information to tags

    project_types = projects.project_type.unique()

    # Create a dictionary to type-specific GeoDataFrames
    components = _create_components_dict(projects, project_types)

    components = _set_electrical_params(components)  # bus types, voltage levels

    components["links_electricity_transmission"] = _set_params_links_electricity(
        components["links_electricity_transmission"]
    )
    components["links_offshore_grids"] = _set_params_links_electricity(
        components["links_offshore_grids"]
    )
    components["lines_electricity_transmission"] = _set_params_lines_electricity(
        components["lines_electricity_transmission"]
    )

    components["links_h2_pipeline"] = _set_params_links_h2(
        components["links_h2_pipeline"]
    )

    # Overwrite parsed data
    components["links_h2_pipeline"] = _map_params_to_projects(
        components["links_h2_pipeline"],
        params_links_h2_pipeline[["pci_code", "p_nom", "source", "comments"]],
    )

    # Split linestrings into segments if they are touched by others
    components["links_h2_pipeline"] = _split_to_segments(
        components["links_h2_pipeline"],
        buffer_radius=1600
    )

    # Clean up topology
    buses_h2 = _create_link_ends(components["links_h2_pipeline"])
    buses_h2 = _create_stations(buses_h2, tol=3300)

    # Drop all lines within buses_h2
    components["links_h2_pipeline"] = _drop_internal_links(components["links_h2_pipeline"], buses_h2)

    # Map links to buses
    components["links_h2_pipeline"] = _extend_links_to_buses(components["links_h2_pipeline"], buses_h2)

    # Drop closed loops
    components["links_h2_pipeline"] = components["links_h2_pipeline"].loc[~components["links_h2_pipeline"].is_closed]

    # Update ids and lengths
    components["links_h2_pipeline"]["pci_code"] = components["links_h2_pipeline"]["tags"].apply(lambda x: x["pci_code"])
    components["links_h2_pipeline"] = _create_unique_ids(components["links_h2_pipeline"])
    components["links_h2_pipeline"] = _calculate_length(components["links_h2_pipeline"])


    # CO2 pipelines
    components["links_co2_pipeline"] = _map_params_to_projects(
        components["links_co2_pipeline"],
        params_links_co2_pipeline[["pci_code", "p_nom", "source", "comments"]],
    )

    components["links_co2_pipeline"] = _split_to_segments(
        components["links_co2_pipeline"]
    )
    
    # Clean up topology
    buses_co2 = _create_link_ends(components["links_co2_pipeline"])
    buses_co2 = _create_stations(buses_co2, tol=1200)

    # Drop all lines within buses_co2
    components["links_co2_pipeline"] = _drop_internal_links(components["links_co2_pipeline"], buses_co2)

    # Map links to buses
    components["links_co2_pipeline"] = _extend_links_to_buses(components["links_co2_pipeline"], buses_co2)

    # Drop closed loops
    components["links_co2_pipeline"] = components["links_co2_pipeline"].loc[~components["links_co2_pipeline"].is_closed]

    # Update ids and lengths
    components["links_co2_pipeline"]["pci_code"] = components["links_co2_pipeline"]["tags"].apply(lambda x: x["pci_code"])
    components["links_co2_pipeline"] = _create_unique_ids(components["links_co2_pipeline"])
    components["links_co2_pipeline"] = _calculate_length(components["links_co2_pipeline"])


    ### Storage units
    # Only keep actual store locations (remove injections)
    # Check for each row in components["stores_co2"] if geometry.coords[0] is in list of coordinates
    components["stores_co2"] = components["stores_co2"][
        components["stores_co2"].geometry.apply(
            lambda row: row.coords[0] in CO2_STORE_LOCATIONS
        )
    ]
    # Remove substring in index behind last "-" and merge substrings
    components["stores_co2"].index = components["stores_co2"].reset_index().id.apply(
        lambda x: "-".join(x.split("-")[:-1]) if len(x.split("-")) > 2 else x
    )
    # Remove substring in index behind last "-" and merge substrings
    components["stores_h2"].index = components["stores_h2"].reset_index().id.apply(
        lambda x: "-".join(x.split("-")[:-1]) if len(x.split("-")) > 2 else x
    )
    # Remove substring in index behind last "-" and merge substrings
    components["storage_units_electricity"].index = components["storage_units_electricity"].reset_index().id.apply(
        lambda x: "-".join(x.split("-")[:-1]) if len(x.split("-")) > 2 else x
    )
    components["storage_units_electricity"] = components["storage_units_electricity"].loc[
        ~components["storage_units_electricity"].index.duplicated(keep="first")
    ]

    # Map params to storage projects
    components["stores_co2"].drop(columns=["e_nom"], inplace=True)
    components["stores_co2"] = _map_params_to_projects(
        components["stores_co2"],
        params_stores_co2
    )
    components["stores_co2"].index = components["stores_co2"]["name"]
    components["stores_co2"].index.name = "id"
    components["stores_co2"].drop(columns=["name"], inplace=True)

    components["stores_h2"].drop(columns=["e_nom"], inplace=True)
    components["stores_h2"] = _map_params_to_projects(
        components["stores_h2"],
        params_stores_h2,
    )
    components["stores_h2"].index = components["stores_h2"]["name"]
    components["stores_h2"].index.name = "id"
    components["stores_h2"].drop(columns=["name"], inplace=True)

    components["storage_units_electricity"].drop(columns=["p_nom", "max_hours", "carrier"], inplace=True)
    components["storage_units_electricity"] = _map_params_to_projects(
        components["storage_units_electricity"],
        params_storage_units_electricity,
    )
    components["storage_units_electricity"].index = components["storage_units_electricity"]["name"]
    components["storage_units_electricity"].index.name = "id"
    components["storage_units_electricity"].drop(columns=["name"], inplace=True)

    # Export to correct output files depending on project_type
    total_count = 0
    for project_type in project_types:
        project_subset = components[project_type]
        project_count = len(
            project_subset["tags"].apply(lambda x: x["pci_code"]).unique()
        )
        logger.info(
            f"Exporting {project_count} {project_type} projects to {snakemake.output[project_type]}"
        )
        project_subset.to_file(snakemake.output[project_type], driver="GeoJSON")
        total_count += project_count
    logger.info(
        f"Exported a total of {total_count} projects. Note that some PCI/PMI project codes contain multiple project types."
    )


# %% Debugging
# import folium
# import branca

# colors = branca.colormap.LinearColormap(
#     colors=["red", "blue", "green", "purple", "orange", "brown"],
#     vmin=0,
#     vmax=len(project_types)-1,
# )
# colormap = {ptype: colors(i) for i, ptype in enumerate(project_types)}

# map = folium.Map(location=[50, 10], zoom_start=5)

# # Iterate over project types and add them to the map
# for i, project_type in enumerate(projects.project_type.unique()):
#     project_subset = projects[projects.project_type == project_type]

#     # Add each project's geometry to the map with customized tooltips
#     map = project_subset.explore(
#         color=colormap[project_type],
#         m=map,
#         name=project_type,
#         tooltip_kwds=dict(
#             style="max-width: 300px; word-wrap: break-word;"
#         )
#     )

# # Add the LayerControl
# folium.LayerControl(position='topright', collapsed=False).add_to(map)

# # Display the map
# map
