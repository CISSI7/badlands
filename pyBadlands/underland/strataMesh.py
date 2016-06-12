##~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~##
##                                                                                   ##
##  This file forms part of the Badlands surface processes modelling application.    ##
##                                                                                   ##
##  For full license and copyright information, please refer to the LICENSE.md file  ##
##  located at the project root, or contact the authors.                             ##
##                                                                                   ##
##~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~##
"""
This module defines the stratigraphic layers based on the TIN nodes.
"""
import os
import glob
import time
import h5py
import numpy
import mpi4py.MPI as mpi
from scipy import interpolate
from scipy.spatial import cKDTree
from scipy.interpolate import RegularGridInterpolator
from pyBadlands.libUtils import FASTloop

class strataMesh():
    """
    This class builds stratigraphic layer on each depositional point of the regular mesh.

    Parameters
    ----------
    variable: stratIn
        Numpy array flaging the presence of a stratigraphic layer for each node

    variable: stratElev
        Numpy array containing the relative elevation of the layer at the time of deposition

    variable: stratThick
        Numpy array containing the thickness of each stratigraphic layer

    variable: stratDepth
        Numpy array containing the current depth of each stratigraphic layer
    """

    def __init__(self, sdx, bbX, bbY, layNb, xyTIN, folder, h5file,
                 cumdiff=0, rfolder=None, rstep=0, regionID=0):
        """
        Constructor.

        Parameters
        ----------
        variable: sdx
            Discretisation value [m]

        variable: bbX
            Extent of stratal regular grid along X

        variable: bbY
            Extent of stratal regular grid along Y

        variable: layNb
            Total number of stratigraphic layers

        variable : xyTIN
            Numpy float-type array containing the coordinates for each nodes in the TIN (in m)

        variable : folder
            Name of the output folder.

        variable: h5file
            First part of the hdf5 file name.

        variable: cumdiff
            Numpy array containing  cumulative erosion/deposition from previous simulation.

        variable: rfolder, rstep
            Restart folder and step.

        variable: regionID
            Stratal domain ID.
        """

        # Initialise MPI communications
        comm = mpi.COMM_WORLD
        rank = comm.Get_rank()
        size = comm.Get_size()

        self.ids = None
        self.ptsNb = None
        self.prevload = 0.
        self.tree = None
        self.folder = folder
        self.h5file = h5file+'.region%s.time'%regionID
        self.step = 0
        self.upper = None
        self.lower = None

        # User defined parameter
        self.dx = sdx

        # Create stratal regular grid
        self.nx = int(round((bbX[1]-bbX[0])/sdx - 0.5)+1)
        self.ny = int(round((bbY[1]-bbY[0])/sdx - 0.5)+1)
        xgrid = numpy.linspace(bbX[0],bbX[1],num=self.nx)
        ygrid = numpy.linspace(bbY[0],bbY[1],num=self.ny)
        xi, yi = numpy.meshgrid(xgrid, ygrid)
        self.xyi = numpy.dstack([xi.flatten(), yi.flatten()])[0]

        # Partition mesh
        self.buildPartition(bbX, bbY)
        self.ptsNb = len(self.ids)

        if rstep > 0:
            if os.path.exists(rfolder):
                folder = rfolder+'/h5/'
                fileCPU = 'sed.region%s.time%s.p*.hdf5'%(regionID,rstep)
                restartncpus = len(glob.glob1(folder,fileCPU))
                if restartncpus == 0:
                    raise ValueError('The requested time step for the restart simulation cannot be found in the restart folder.')
            else:
                raise ValueError('The restart folder is missing or the given path is incorrect.')

            if restartncpus != size:
                raise ValueError('When using the stratal model you need to run the restart simulation with the same number of processors as the previous one.')

            df = h5py.File('%s/h5/sed.region%s.time%s.p%s.hdf5'%(rfolder, regionID, rstep, rank), 'r')
            layDepth = numpy.array((df['/layDepth']))
            layElev = numpy.array((df['/layElev']))
            layThick = numpy.array((df['/layThick']))
            rstlays = layDepth.shape[1]
            layNb +=  rstlays
            self.step = rstlays

        # Define global stratigraphic dataset
        self.stratIn = numpy.zeros([self.ptsNb],dtype=int)
        self.stratElev = numpy.zeros([self.ptsNb,layNb])
        self.stratThick = numpy.zeros([self.ptsNb,layNb])
        self.stratDepth = numpy.zeros([self.ptsNb,layNb])

        if rstep > 0:
            self.stratDepth[:,:rstlays] = layDepth
            self.stratElev[:,:rstlays] = layElev
            self.stratThick[:,:rstlays] = layThick

        # Define TIN grid kdtree for interpolation
        self.tree = cKDTree(xyTIN)
        tindx = xyTIN[1,0] - xyTIN[0,0]
        self.searchpts = max(int(sdx*sdx/(tindx*tindx)),4)

        if rstep > 0:
            distances, indices = self.tree.query(self.xyi, k=self.searchpts)

            if len(cumdiff[indices].shape) == 3:
                cum_vals = cumdiff[indices][:,:,0]
            else:
                cum_vals = cumdiff[indices]
            fcum = numpy.average(cum_vals,weights=(1./distances), axis=1)
            onIDs = numpy.where(distances[:,0] == 0)[0]
            if len(onIDs) > 0:
                fcum[onIDs] = cumdiff[indices[onIDs,0]]
            self.prevload = fcum

        return

    def update_TIN(self, xyTIN):
        """
        Update stratal mesh after 3D displacements.

        variable : xyTIN
            Numpy float-type array containing the coordinates for each nodes in the TIN (in m)
        """

        # Update TIN grid kdtree for interpolation
        self.tree = cKDTree(xyTIN)

        return

    def move_mesh(self, dispX, dispY, verbose=False):
        """
        Update stratal mesh after 3D displacements.

        variable : dispX
            Numpy float-type array containing X-displacement for each nodes in the stratal mesh

        variable : dispY
            Numpy float-type array containing Y-displacement for each nodes in the stratal mesh
        """

        # Initialise MPI communications
        comm = mpi.COMM_WORLD
        rank = comm.Get_rank()
        size = comm.Get_size()

        # Move coordinates
        walltime = time.clock()
        st_time = walltime
        moveXY = numpy.zeros([self.xyi.shape[0],2])
        moveXY[:,0] = self.xyi[:,0] + dispX
        moveXY[:,1] = self.xyi[:,1] + dispY

        # Define point ids in local partition which needs to be send to neighbourhood
        l0 = self.nx
        l1 = l0 + self.nx
        u1 = self.ptsNb - self.nx
        u0 = u1 - self.nx
        shape = (l1-l0, self.step+1)

        if rank == 0 and verbose:
            print " - move stratal mesh ", time.clock() - walltime

        if size > 1:
            walltime = time.clock()
            if rank == 0:
                # Send upper row to next processor
                uData1 = self.stratThick[u0:u1,:self.step+1].ravel()
                uData2 = self.stratElev[u0:u1,:self.step+1].ravel()
                uData3 = self.prevload[u0:u1]
                comm.Send(uData1, dest=rank+1, tag=rank)
                comm.Send(uData2, dest=rank+1, tag=rank+2000)
                comm.Send(uData3, dest=rank+1, tag=rank+4000)
                # Receive upper ghost row from next processor
                comm.Recv(guData1, source=rank+1, tag=rank+1)
                comm.Recv(guData2, source=rank+1, tag=rank+2001)
                comm.Recv(guData3, source=rank+1, tag=rank+4001)
                guData1.reshape(shape)
                guData2.reshape(shape)

            elif rank == size-1:
                # Send lower row to previous processor
                lData1 = self.stratThick[l0:l1,:self.step+1].ravel()
                lData2 = self.stratElev[l0:l1,:self.step+1].ravel()
                lData3 = self.prevload[l0:l1]
                comm.Send(lData1, dest=rank-1, tag=rank)
                comm.Send(lData2, dest=rank-1, tag=rank+2000)
                comm.Send(lData3, dest=rank-1, tag=rank+4000)
                # Receive lower ghost row from previous processor
                comm.Recv(glData1, source=rank-1, tag=rank-1)
                comm.Recv(glData2, source=rank-1, tag=rank+1999)
                comm.Recv(glData3, source=rank-1, tag=rank+3999)
                glData1.reshape(shape)
                glData2.reshape(shape)

            else:
                # Send lower row to previous processor
                lData1 = self.stratThick[l0:l1,:self.step+1].ravel()
                lData2 = self.stratElev[l0:l1,:self.step+1].ravel()
                lData3 = self.prevload[l0:l1]
                comm.Send(lData1, dest=rank-1, tag=rank)
                comm.Send(lData2, dest=rank-1, tag=rank+2000)
                comm.Send(lData3, dest=rank-1, tag=rank+4000)
                # Receive lower ghost row from previous processor
                comm.Recv(glData1, source=rank-1, tag=rank-1)
                comm.Recv(glData2, source=rank-1, tag=rank+1999)
                comm.Recv(glData3, source=rank-1, tag=rank+3999)
                glData1.reshape(shape)
                glData2.reshape(shape)
                # Send upper row to next processor
                uData1 = self.stratThick[u0:u1,:self.step+1].ravel()
                uData2 = self.stratElev[u0:u1,:self.step+1].ravel()
                uData3 = self.prevload[u0:u1]
                comm.Send(uData1, dest=rank+1, tag=rank)
                comm.Send(uData2, dest=rank+1, tag=rank+2000)
                comm.Send(uData3, dest=rank+1, tag=rank+4000)
                # Receive upper ghost row from next processor
                comm.Recv(guData1, source=rank+1, tag=rank+1)
                comm.Recv(guData2, source=rank+1, tag=rank+2001)
                comm.Recv(guData3, source=rank+1, tag=rank+4001)
                guData1.reshape(shape)
                guData2.reshape(shape)

        # Build deformed mesh
        if size > 1:
            u0 = self.upper[rank,0]
            u1 = self.upper[rank,1]
            l0 = self.lower[rank,0]
            l1 = self.lower[rank,1]
            if rank == 0:
                deformXY = numpy.concatenate((moveXY[self.ids,:], moveXY[u0:u1,:]), axis=0)
                deformThick = numpy.concatenate((self.stratThick[:,:self.step+1], guData1), axis=0)
                deformElev = numpy.concatenate((self.stratElev[:,:self.step+1], guData2), axis=0)
                deformLoad = numpy.concatenate((self.prevload, guData3), axis=0)
            elif rank == size-1:
                deformXY = numpy.concatenate((moveXY[self.ids,:], moveXY[l0:l1,:]), axis=0)
                deformThick = numpy.concatenate((self.stratThick[:,:self.step+1], glData1), axis=0)
                deformElev = numpy.concatenate((self.stratElev[:,:self.step+1], glData2), axis=0)
                deformLoad = numpy.concatenate((self.prevload, glData3), axis=0)
            else:
                deformXY = numpy.concatenate((moveXY[self.ids,:], moveXY[l0:l1,:]), axis=0)
                deformXY = numpy.concatenate((deformXY, moveXY[u0:u1,:]), axis=0)
                deformThick = numpy.concatenate((self.stratThick[:,:self.step+1], glData1), axis=0)
                deformElev = numpy.concatenate((self.stratElev[:,:self.step+1], glData2), axis=0)
                deformLoad = numpy.concatenate((self.prevload, glData3), axis=0)
                deformThick = numpy.concatenate((deformThick, guData1), axis=0)
                deformElev = numpy.concatenate((deformElev, guData2), axis=0)
                deformLoad = numpy.concatenate((deformLoad, guData3), axis=0)

            if rank == 0 and verbose:
                print " - send/receive communication stratal mesh ", time.clock() - walltime
        else:
            walltime = time.clock()
            deformXY = moveXY
            deformThick = self.stratThick[:,:self.step+1]
            deformElev = self.stratElev[:,:self.step+1]
            deformLoad = self.prevload
            if rank == 0 and verbose:
                print " - create deformed stratal mesh arrays ", time.clock() - walltime

        # Build the kd-tree
        walltime = time.clock()
        deformtree = cKDTree(deformXY)
        if rank == 0 and verbose:
            print " - create deformed stratal mesh kd-tree ", time.clock() - walltime

        walltime = time.clock()
        distances, indices = deformtree.query(self.xyi, k=4)
        if rank == 0 and verbose:
            print " - query stratal mesh kd-tree ", time.clock() - walltime

        # Compute inverse weighting distance
        walltime = time.clock()
        w = 1.0 / distances**2
        w3D = w.reshape((len(self.xyi),4,1))
        weights = numpy.tile(w3D, (1,1,self.step+1))

        # Perform interpolation
        tmpIDs = numpy.where(distances[:,0] == 0)[0]
        if len(tmpIDs) > 0:
            self.stratThick[tmpIDs,:self.step+1] = deformElev[indices[tmpIDs,0],:self.step+1]
            self.stratElev[tmpIDs,:self.step+1]  = deformThick[indices[tmpIDs,0],:self.step+1]
            tmpID = numpy.where(distances[:,0] > 0)[0]
            self.stratThick[tmpID,:self.step+1] = numpy.average(deformThick[indices[tmpID,:],:],
                                                                weights=weights[tmpID,:], axis=1)
            self.stratElev[tmpID,:self.step+1] = numpy.average(deformElev[indices[tmpID,:],:],
                                                                weights=weights[tmpID,:], axis=1)

        else:
            self.stratThick[:,:self.step+1] = numpy.average(deformThick[indices,:],weights=weights, axis=1)
            self.stratElev[:,:self.step+1] = numpy.average(deformElev[indices,:],weights=weights, axis=1)

        # Reset depostion flag
        self.stratIn.fill(0)
        tmpID = numpy.where(numpy.amax(self.stratThick[:,:self.step+1], axis=1)>0)[0]
        self.stratIn[tmpID] = 1
        if rank == 0 and verbose:
            print " - perform stratal mesh interpolation ", time.clock() - walltime

        # Apply displacements to previous load
        if len(deformLoad[indices].shape) == 3:
            self.prevload = numpy.average(deformLoad[indices][:,:,0],weights=w, axis=1)
        else:
            self.prevload = numpy.average(deformLoad[indices],weights=w, axis=1)
        if len(tmpIDs) > 0:
            self.prevload[tmpIDs] = deformLoad[indices[tmpIDs,0]]


        if rank == 0 and verbose:
            print " - moving stratal mesh function ", time.clock() - st_time

        return

    def buildStrata(self, elev, cumdiff, sea, rank, write=0, outstep=0):
        """
        Build the stratigraphic layer on the regular grid.

        variable : elev
            Numpy float-type array containing the elevation of the nodes in the TIN

        variable : cumdiff
            Numpy float-type array containing the cumulative erosion/deposition of the nodes in the TIN

        variable : sea
            Sea level elevation

        variable : rank
            Rank of the given processor

        variable : write
            Flag for output generation

        variable : outstep
            Step for output generation
        """

        selev = numpy.zeros(len(self.xyi))
        scumload = numpy.zeros(len(self.xyi))
        distances, indices = self.tree.query(self.xyi, k=self.searchpts)

        if len(elev[indices].shape) == 3:
            elev_vals = elev[indices][:,:,0]
            cum_vals = cumdiff[indices][:,:,0]
        else:
            elev_vals = elev[indices]
            cum_vals = cumdiff[indices]

        felev = numpy.average(elev_vals,weights=(1./distances), axis=1)
        fcum = numpy.average(cum_vals,weights=(1./distances), axis=1)
        onIDs = numpy.where(distances[:,0] == 0)[0]
        if len(onIDs) > 0:
            felev[onIDs] = elev[indices[onIDs,0]]
            fcum[onIDs] = cumdiff[indices[onIDs,0]]
        scumload = fcum - self.prevload
        self.prevload = fcum
        selev = felev

        # Update stratal elevation
        self.stratElev[self.ids,self.step] =  selev[self.ids]-sea

        # Update stratal deposition
        localCum = scumload[self.ids]
        depIDs = numpy.where(localCum>0.)[0]
        self.depoLayer(self.ids[depIDs], localCum)

        # Update stratal erosion
        eroIDs = numpy.where(localCum<0.)[0]
        self.eroLayer(self.ids[eroIDs], localCum)

        if write>0:
            self.layerMesh(selev[self.ids])
            self.write_hdf5_stratal(outstep,rank)

        self.step += 1

        return

    def buildPartition(self, bbX, bbY):
        """
        Define a partition for the stratal mesh.
        """

        # Initialise MPI communications
        comm = mpi.COMM_WORLD
        rank = comm.Get_rank()
        size = comm.Get_size()

        # extent of X partition
        Yst = numpy.zeros( size )
        Yed = numpy.zeros( size )
        partYID = numpy.zeros( (size,2) )
        nbY = int((self.ny-1) / size)
        for p in range(size):
            if p == 0:
                Yst[p] = bbY[0]
                Yed[p] = Yst[p] + nbY*self.dx
                partYID[p,0] = 0
                partYID[p,1] = (nbY+1)*self.nx
            else:
                Yst[p] = Yed[p-1]
                Yed[p] = Yst[p] + nbY*self.dx
                partYID[p,0] = partYID[p-1,1] - self.nx
                partYID[p,1] = partYID[p,0] + (nbY+1)*self.nx
        Yed[size-1] = bbY[1]
        partYID[size-1,1] = self.ny*self.nx

        # Get send/receive data ids for each processors
        self.upper = numpy.zeros( (size,2) )
        self.lower = numpy.zeros( (size,2) )
        self.upper[rank,0] = partYID[rank,1]
        self.upper[rank,1] = partYID[rank,1] + self.nx
        self.lower[rank,0] = partYID[rank,0]
        self.lower[rank,1] = partYID[rank,0] - self.nx

        # Define partitions ID globally
        Xst = numpy.zeros( size )
        Xed = numpy.zeros( size )
        Xst += bbX[0]
        Xed += bbX[1]

        # Loop over node coordinates and find if they belong to local partition
        # Note: used a Cython/Fython class to increase search loop performance... in libUtils
        partID = FASTloop.part.overlap(self.xyi[:,0],self.xyi[:,1],Xst[rank],
                                        Yst[rank],Xed[rank],Yed[rank])

        # Extract local domain nodes global ID
        self.ids = numpy.where(partID > -1)[0]

        return

    def depoLayer(self, ids, depo):
        """
        Add deposit to current stratigraphic layer.

        Parameters
        ----------
        variable: ids
            Index of points subject to deposition

        variable: depo
            Value of the deposition for the given point [m]
        """

        # Initialise node deposition flag
        tmpIDs = numpy.where(self.stratIn[ids]==0)
        self.stratIn[ids[tmpIDs]] = 1

        # Add deposit to the considered layer time
        self.stratThick[ids,self.step] += depo[ids]

        return

    def eroLayer(self, nids, erosion):
        """
        Erode top stratigraphic layers.

        Parameters
        ----------
        variable: nids
            Index of points subject to erosion

        variable: erosion
            Value of the erosion for the given points [m]
        """

        # Perform erosion on nodes containing stratigraphic layers
        tmpIDs = numpy.where(self.stratIn[nids] == 1)[0]
        if len(tmpIDs) == 0:
            return

        # Update node indices and associated erosion values
        ids = nids[tmpIDs]
        ero = -erosion[ids]

        # Compute cumulative stratal thicknesses
        cumThick = numpy.cumsum(self.stratThick[ids,self.step::-1],axis=1)[:,::-1]

        # Find nodes with no remaining stratigraphic thicknesses
        tmpIDs = numpy.where(ero>=cumThick[:,0])[0]
        self.stratIn[ids[tmpIDs]] = 0
        self.stratThick[ids[tmpIDs],:self.step+1] = 0.

        # Erode remaining stratal layers
        if len(tmpIDs) < len(ids):
            ero[tmpIDs] = 0.

            # Clear all stratigraphy points which are eroded
            cumThick[cumThick < ero.reshape((len(ids),1))] = 0
            mask = (cumThick > 0).astype(int) == 0
            tmpH = self.stratThick[ids,:self.step+1]
            tmpH[mask] = 0
            self.stratThick[ids,:self.step+1] = tmpH

            # Update thickness of top stratigraphic layer
            eroIDs = numpy.bincount(numpy.nonzero(cumThick)[0]) - 1
            eroVals = cumThick[numpy.arange(len(ids)),eroIDs]-ero
            eroVals[tmpIDs] = 0.
            self.stratThick[ids,eroIDs] = eroVals

        return

    def layerMesh(self, topsurf):
        """
        Define stratigraphic layers mesh.

        Parameters
        ----------

        variable: topsurf
            Elevation of the regular surface
        """

        # Clear points with no stratigraphic layer
        tmpIDs = numpy.where(self.stratIn == 0)[0]
        #surf = numpy.tile(topsurf[tmpIDs].transpose(), (1, self.step+1)).reshape(self.step+1,len(tmpIDs)).transpose()
        surf = numpy.array([topsurf[tmpIDs],]*int(self.step+1)).transpose()
        self.stratDepth[tmpIDs,:self.step+1] = surf
        self.stratThick[tmpIDs,:self.step+1] = 0.

        # Find points with stratigraphic layers
        tmpIDs = numpy.where(self.stratIn == 1)[0]
        if len(tmpIDs) == 0:
            return

        # Compute cumulative stratal thicknesses
        cumThick = numpy.cumsum(self.stratThick[tmpIDs,self.step::-1],axis=1)[:,::-1]

        # Updata stratal depth
        #surf = numpy.tile(topsurf[tmpIDs].transpose(), (1, self.step+1)).reshape(self.step+1,len(tmpIDs)).transpose()
        surf = numpy.array([topsurf[tmpIDs],]*int(self.step+1)).transpose()
        self.stratDepth[tmpIDs,:self.step+1] = surf - cumThick

        return

    def write_hdf5_stratal(self, outstep, rank):
        """
        This function writes for each processor the HDF5 file containing sub-surface information.

        Parameters
        ----------

        variable : outstep
            Output time step.

        variable : rank
            ID of the local partition.
        """

        sh5file = self.folder+'/'+self.h5file+str(outstep)+'.p'+str(rank)+'.hdf5'
        with h5py.File(sh5file, "w") as f:

            # Write node coordinates
            f.create_dataset('coords',shape=(self.ptsNb,2), dtype='float32', compression='gzip')
            f["coords"][:,:2] = self.xyi[self.ids]

            # Write stratal layers depth per cells
            f.create_dataset('layDepth',shape=(self.ptsNb,self.step+1), dtype='float32', compression='gzip')
            f["layDepth"][:,:self.step+1] = self.stratDepth[self.ids,:self.step+1]

            # Write stratal layers elevations per cells
            f.create_dataset('layElev',shape=(self.ptsNb,self.step+1), dtype='float32', compression='gzip')
            f["layElev"][:,:self.step+1] = self.stratElev[self.ids,:self.step+1]

            # Write stratal layers thicknesses per cells
            f.create_dataset('layThick',shape=(self.ptsNb,self.step+1), dtype='float32', compression='gzip')
            f["layThick"][:,:self.step+1] = self.stratThick[self.ids,:self.step+1]

        return
