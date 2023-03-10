from shapely.geometry import Point, Polygon, MultiPoint
from shapely.ops import nearest_points
import geopandas as gpd
import pandas as pd
import folium
import uuid
from datetime import date
import json
import openrouteservice
import geojson
import os
from vincenty import vincenty

from dotenv import load_dotenv
load_dotenv('.env')


####
#### functions to read/write data to/from files
####

def csv_to_gdf(filepath: str, crs: str = 'epsg:31370' ):
    df = pd.read_csv(filepath, nrows=0)

    if not set(['id', 'latitude', 'longitude']).issubset(df.columns):
        s = ','.join(map(str, list(df.columns)))
        raise ValueError(
            f'Wrong column names, columns should be called id, latitude, longitude, received {s}')

    df = pd.read_csv(filepath)

    if not 'uuid' in df.columns:
        df['uuid'] = df.apply(lambda x: uuid.uuid4(), axis=1)

    geometry = [Point(lon, lat)
                for lon, lat in zip(df['longitude'], df['latitude'])]

    gdf = gpd.GeoDataFrame(df, geometry=geometry,crs='EPSG:4326')

    gdf = gdf.to_crs(crs)

    return gdf


def geojson_to_gdf(crs: str = 'epsg:31370' ):

    file_path = "./geojson/"

    if not os.path.exists(file_path):
        os.mkdir('./geojson/')

    file_list = list()
    for file in os.listdir(file_path):
        if file.endswith('.geojson'):
            file_list.append(file)

    if not file_list:
        raise FileNotFoundError("No geojson files found in the directory")

    file_list = [file_path + f for f in file_list]

    gdf = pd.concat([gpd.read_file(file, crs='epsg:4326')
                    for file in file_list]).set_index('id')

    # gdf["longitude"] = gdf.centroid.x
    # gdf["latitude"] = gdf.centroid.y

    gdf = gdf.to_crs(crs)

    gdf["centroid"] = gdf.centroid

    return gdf


def fetch_drivetime(poi, time_range: int):
    # authentication OpenRouteService
    secret = os.getenv("ORS_SECRET")
    ors = openrouteservice.Client(key=secret)

    isochrone = ors.isochrones(
        locations=[[poi['longitude'], poi['latitude']]], range=[time_range])

    properties = isochrone['features'][0]['properties']
    properties['id'] = poi['id']
    properties['longitude'] = poi['longitude']
    properties['latitude'] = poi['latitude']

    with open(f"./geojson/{poi['id']}_{date.today()}.geojson", 'w') as output_file:
        json.dump(isochrone, output_file, ensure_ascii=False, indent=4)


####
#### functions to filter point list
####

def fetch_points_in_polygon(polygon, points):
    # TODO add checks for polygon; is it in geojson or in dataframe format?
    if not isinstance(points, (gpd.GeoDataFrame, pd.DataFrame)):
        raise TypeError("points should be a valid GeoDataFrame or DataFrame")
    if 'geometry' not in points.columns:
        raise AttributeError("points should have a 'geometry' column")
    if not all(points['geometry'].apply(lambda x: x.is_valid)):
        raise ValueError("'geometry' column of the points dataframe should contain only valid geometries")

    gdf = points.loc[(points['geometry'].within(polygon) | points['geometry'].touches(polygon)), 
    ['longitude', 'latitude', 'uuid', 'geometry']]

    return gdf


def fetch_x_points_in_polygons():
    # TODO limit the amount of points returned
    # selection should be as homogenous as possible
    # might have to change the function above instead
    pass


####
#### functions to reduce point clustering in polygon
####

def create_buffer_gsr(points, range_in_meters: int, crs: str):

    if not isinstance(points, (gpd.GeoDataFrame, pd.DataFrame)):
        raise TypeError("points should be a valid GeoDataFrame or DataFrame")
    if not isinstance(range_in_meters, (int, float)):
        raise TypeError("range_in_meters should be a valid number")
    if not isinstance(crs, str):
        raise TypeError("crs should be a valid proj4 string")
    if 'geometry' not in points.columns:
        raise AttributeError("points should have a 'geometry' column")
    if not all(points['geometry'].apply(lambda x: x.is_valid)):
        raise ValueError("'geometry' column of the points dataframe should contain only valid geometries")
    if range_in_meters < 0:
        raise ValueError("range_in_meters should be a positive number")

    # dataframes are passed by reference thus a copy must be created to avoid modifying the original dataframe
    points = points.copy()

    points.crs = crs

    buffer = points.geometry.buffer(range_in_meters)

    buffer_union = buffer.geometry.unary_union

    buffer = gpd.GeoSeries(buffer_union, crs=crs)
    # note from the docs:
    # index_parts
    # boolean, default True
    # If True, the resulting index will be a multi-index (original index with an additional level indicating the multiple geometries: a new zero-based index for each single part geometry per multi-part geometry).
    buffer = buffer.explode(index_parts=False)

    uuid_list = [uuid.uuid4() for _ in range(len(buffer))]

    buffer.index = uuid_list

    return buffer

def get_corresponding_buffer_id(point,buffer):
    for index,buffer_zone in buffer.items():
        if point.within(buffer_zone):
            return index

def delete_point_closest_to_centroid(points,buffer):

    points['buffer_id'] = points['geometry'].apply(lambda x :
    get_corresponding_buffer_id(x,buffer))

    # create a buffer

    for buffer_id, buffer_geometry in buffer.items():
        points_in_current_buffer = points.loc[points['buffer_id'] == buffer_id]
        if len(points_in_current_buffer) > 1:
            dist_to_centroid = points_in_current_buffer.geometry.distance(buffer_geometry.centroid)

            closest_to_centroid = dist_to_centroid.idxmin()

            points.drop(closest_to_centroid,axis=0,inplace=True)
    
    return points


def reduce_point_clustering(points, crs: str, range_in_meters: int =100):
    points = points.copy()

    points_buffer = gpd.GeoSeries()

    while len(points_buffer) != len(points):
        points_buffer = create_buffer_gsr(points, range_in_meters, crs)
        points['buffer_id'] = points['geometry'].apply(lambda x : get_corresponding_buffer_id(x,points_buffer))

        points = delete_point_closest_to_centroid(points,points_buffer)

    return points

####
#### functions to calculate distance to poi
####

# based on get_nearest_values from https://autogis-site.readthedocs.io/en/2019/notebooks/L3/nearest-neighbour.html
def get_closest_poi_id_to_point(row, other_gdf,point_column="geometry", other_gdf_point_column="geometry"):
    # Create an union of the other GeoDataFrame's geometries:
    other_points = other_gdf[other_gdf_point_column].unary_union

    # Find the nearest points
    nearest_geoms = nearest_points(row[point_column], other_points)
    
    # Get corresponding values from the other df
    nearest_data = other_gdf.loc[other_gdf[other_gdf_point_column] == nearest_geoms[1]]

    nearest_value = nearest_data.index.values[0]
    
    return nearest_value


def get_dist_to_point(lat1,lon1, lat2, lon2):

    point1 = (lat1, lon1)
    point2 = (lat2, lon2)

    return vincenty(point1, point2)

def add_nearest_poi_distance(points,poi):
    # get average distance of a point to the poi closest to them
    # 1. link each point to the poi closest to them and store distance to poi
    # 2. get average from this
    points = points.copy()

    points['nearest_poi'] = points.apply(get_closest_poi_id_to_point, other_gdf=poi, other_gdf_point_column="centroid", axis=1)

    points['dist_to_poi'] = points.apply(lambda x : get_dist_to_point(x['latitude'], x['longitude'],poi.loc[x['nearest_poi']]['latitude'],poi.loc[x['nearest_poi']]['longitude']),axis=1)

    return points


def get_avg_distance(points, column_name) -> float:
    return points[column_name].mean()


# TODO:
def get_percentage_of_points_within_poi_dist(distance: float) -> float:
    # fetch_points_in_polygon is related to this

    pass

def get_count_of_points_in_poi():
    # adds a column x_km_coverage with count of points in said distance
    pass
 

##########################

points = csv_to_gdf("./dummy/p.csv")

# print(create_buffer_gsr(gdf,300,'epsg:31370'))

# with open('./dummy/test_geo.geojson', 'r') as f:
#     gj = geojson.load(f)

# dt = Polygon(gj['features'][0]['geometry']['coordinates'][0])

poi = geojson_to_gdf()


# fetch_points_in_drivetime(dt, gdf)

points_with_dist_to_poi = add_nearest_poi_distance(points,poi)

print(get_avg_distance(points_with_dist_to_poi,'dist_to_poi'))

print(points_with_dist_to_poi.tail())


poi = {
    'longitude':  4.481953941484154,
    'latitude':  51.024599008502776,
    'id': 'Mechelen'
}


# fetch_drivetime(poi, 900)

# ref: https://www.ics.uci.edu/~eppstein/gina/scot.drysdale.html#dt
# ref: https://towardsdatascience.com/how-to-create-voronoi-regions-with-geospatial-data-in-python-adbb6c5f2134
# ref: https://stackoverflow.com/questions/9600801/evenly-distributing-n-points-on-a-sphere
# ref: https://stackoverflow.com/questions/22228946/choose-n-most-evenly-spread-points-across-point-dataset-in-r

# ref: https://towardsdatascience.com/the-fascinating-world-of-voronoi-diagrams-da8fc700fa1b
# lloyd's algorithm seems pretty interesting but with an additional step of deleting smallest polygons, or creating just the amount of polygons necessary for the n subset of points

# ref: https://gis.stackexchange.com/questions/321021/splitting-polygon-into-equal-area-polygons-using-qgis
# this seems to be the most relevant one