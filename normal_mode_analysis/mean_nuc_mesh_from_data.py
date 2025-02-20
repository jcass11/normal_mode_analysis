#import datasetdatabase as dsdb
import numpy as np
import pandas as pd

import vtk
from stl import mesh

from skimage import io as skio
from skimage import measure

from mpl_toolkits import mplot3d
import matplotlib.pyplot as plt
from PIL import Image, ImageDraw

import meshcut

import imageio
import os

from tqdm import tqdm_notebook as tqdm
from joblib import Parallel, delayed


def create_csv_from_database():
	"""Download nuclear mesh databse with dsdb and save in repo.
	:return: dataframe containing downloaded timelapse nucleus segmentations.
	"""

	prod = dsdb.DatasetDatabase(config="~/.config_dsdb.json")
	df = prod.get_dataset(id=304).ds
	df.to_csv("mesh_vtk_files/nucleus_timelapse.csv")
	
	return df


def get_mesh_polydata(df, i):
		"""Read database to get one mesh as polydata.
        :param df: dataframe of nucleus segmentations
		:param i: index of segmentation in dataset
		:return polydata mesh for this segmentation
		"""

		reader = vtk.vtkPolyDataReader()
		# read in a specific file
		reader.SetFileName('nucleus_mesh_data/mesh_vtk_files/'+df['CellId'][i]+'.vtk')
		reader.Update()
		# get data out of file
		polydata = reader.GetOutput()
        
		return polydata
    
def get_binary_mask_from_mesh(polydata, imsize, save_flag = False):
    """Given a polydata mesh, create a binary 3D mask, centered on the mask image center.
	:param polydata: vtk 3D mesh
    :return 3d mask array:
	"""
    
    # Create a empty template mask
    nx = imsize[0]
    ny = imsize[1]
    nz = imsize[2]
    mask = np.zeros((nz,ny,nx), dtype=np.uint8)
    yy, xx = np.meshgrid(np.arange(ny),np.arange(nx))
    x = xx.flatten()
    y = yy.flatten()

    n_verts = len(y)
    vert_is_inside = np.zeros(n_verts, dtype=np.uint8)

    xypts = vtk.vtkPoints()
    xypts.SetNumberOfPoints(n_verts)
    for i in range(n_verts):
        xypts.SetPoint(i,[x[i],y[i],0])

    # Loop over Z in parallel
    def ProcessThisPlane(mask, z):

        for i in range(n_verts):
            xypts.SetPoint(i,[x[i],y[i],z])

        plane = vtk.vtkPolyData()
        plane.SetPoints(xypts)
        plane.Modified()
        encPoints = vtk.vtkSelectEnclosedPoints()
        encPoints.SetTolerance(1e-6)
        encPoints.SetInputData(plane)
        encPoints.SetSurfaceData(polydata)
        encPoints.Update()

        for i in range(n_verts):
            vert_is_inside[i] = encPoints.IsInside(i)

        mask[z,y,x] = 255*vert_is_inside

    Parallel(n_jobs=2, backend="threading")(
        delayed(ProcessThisPlane)(mask, z) for z in tqdm(range(nz))
    )

    if save_flag:
        skio.imsave('nucleus_mask_data/'+df['CellId'][i]+'.tif', mask)
    return mask


def get_mask_from_mesh(polydata, imsize, dz):
	"""Given a polydata mesh, create a binary 3D mask, centered on the mask image center.
	:param polydata: vtk 3D mesh
	:param imsize: size of desired binary mask image
	:param dz: spacing of z slices through mesh used to create mask
	"""

	def get_faces(i, polydata):
		"""Get vertices of nuclear mesh.
		:param i: index of face in polydata mesh
		:param polydata: mesh
		:return: array of mesh faces , where each face is a list of indices of connected vertices
		"""

		cell = polydata.GetCell(i)
		ids = cell.GetPointIds()
		return np.array([ids.GetId(j) for j in range(ids.GetNumberOfIds())])

	# Collect all faces and vertices into arrays and shift vertices to be centered with mask image
	faces = np.array([get_faces(i, polydata) for i in range(polydata.GetNumberOfCells())])
	verts = np.array([np.array(polydata.GetPoint(i)) for i in range(polydata.GetNumberOfPoints())])
	verts_shift = verts + imsize/2

	z_list = [zslice[2] for zslice in verts_shift]
	zmin = min(z_list)
	zmax = max(z_list)
	
	def get_zslice(z_ind):
		"""Use meshcut to get z slices of mesh, create 2D masks of each slice, and combine slices to get 3D mask.
		:param z_ind: index of z slice mask to cut from mesh
		:return: 2D image as binary mask of z slice at this index
		"""

		im = Image.fromarray(np.zeros((imsize,imsize), np.uint8).T)

		if zmin < z_ind < zmax:
			cut = meshcut.cross_section(verts_shift, faces, [0,0,z_ind], [0,0,1])
			polygon = cut[0][:, :2]

			draw = ImageDraw.Draw(im)
			draw.polygon(polygon.round().astype(np.uint8).flatten().tolist(), fill=255)

		return np.array(im)/255

	full_shape = tuple(get_zslice(z) for z in np.linspace(0, imsize, np.round(imsize/dz)))
	mask = np.dstack(full_shape)

	return mask

	
def get_mean_mask(df, imsize):
	"""Given many meshes in df, find the average binary image mask of all meshes.
	:param df: dataframe containing segmentation filepaths
	:param imsize: size of binary mask image
	:param dz: resolution of z slices
	:return: 3D mean nuclear mask image array
	"""

	polydata = get_mesh_polydata(df, 0)
	# sum_mask = get_mask_from_mesh(polydata, imsize, dz)
	sum_mask = get_binary_mask_from_mesh(polydata, imsize)
   
	for i in range(df.shape[0]):
		polydata = get_mesh_polydata(df, i)
		#sum_mask = np.add(sum_mask, get_mask_from_mesh(polydata, imsize, dz))
		sum_mask = np.add(sum_mask, get_binary_mask_from_mesh(polydata, imsize))
		
	mean_mask = np.divide(sum_mask, df.shape[0])

	np.save('nucleus_mesh_data/mean_nuc_mask', mean_mask)
	return mean_mask


def fix_z(verts, dz, imsize):
	"""Fix z coordinates by rescaling from integers to physical values
	:param verts: mesh vertices
	:param dz: true z spacing of slices
	:param imsize: original image size in z
	:return: rescale vertices with correct z coordinates
	"""

	nz = np.round(imsize/dz)
	dz = imsize/nz
	for vert in verts:
		vert[2] *= dz
	return verts
	
	
def get_mean_mesh(mask, ss=1):
	"""Get mean mesh from mean mask, correcting z coordinates from zslice indexes to spatial values consistent with x/y.
	:param mask: 3D binary image mask of average nuclear shape
	:return: vertices and faces of mesh generated from mask
	"""

	verts, faces, normals, values = measure.marching_cubes_lewiner(mask, step_size=ss)
	return verts, faces
	

def get_mean_mesh_from_individual_meshes(df, imsize):
	"""Master function, getting mean mesh shape from df of meshes.
	:param df: dataframe containing segmentation dataset
	:param imsize: size of binary mask image
	:param dz: resolution of z slices
	:return: vertices and faces of average nuclear mesh
	"""

	mask = get_mean_mask(df, imsize)
	verts, faces = get_mean_mesh(mask, imsize)
	return verts, faces, mask


def plot_nuc_mask(mask, title=None, az=None):
	"""Plut mask in stripes on 3D axes.
	:param mask: 3D binary image mask of nucleus
	:param title: title of file to save figure to, mostly used for genreating movie
	"""

	fig = plt.figure()
	ax = plt.axes(projection="3d")
	if az is not None:
		ax.view_init(azim=az)

	verts, faces, normals, values = measure.marching_cubes_lewiner(mask)
	verts = fix_z(verts, dz, imsize)
	x, y, z = verts.T
	ax.plot_trisurf(x, y, faces, z, lw=0, cmap=plt.cm.Paired)

	plt.tight_layout()
	if title is not None:
		plt.savefig(title, format='png')
	
	
def make_nuc_video(mask, filename):
	"""Generate movie of striped mask rotating around z-axis.
	:param mask: 3D binary image mask of nucleus
	:param filename: name of file to save video to
	"""

	images = []
	for i in np.linspace(0, 360, 25):
		filename = 'az_'+str(int(i))+'.png'
		plot_nuc_mask(mask, filename, i)
		images.append(imageio.imread(filename))
		os.remove(filename)
	imageio.mimsave(filename+'.gif', images)


def save_mesh_as_stl(verts, faces, fname):
	
	# Create the stl Mesh object
	nuc_mesh = mesh.Mesh(np.zeros(faces.shape[0], dtype=mesh.Mesh.dtype))
	for i, f in enumerate(faces):
		for j in range(3):
			nuc_mesh.vectors[i][j] = verts[f[j],:]

	# Write the mesh to file"
	nuc_mesh.save(fname+'.stl')
