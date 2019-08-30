#!/usr/bin/env python
#-------------------------------------------------------------------------------
# Name:     BayesianTracker
# Purpose:  A multi object tracking library, specifically used to reconstruct
#           tracks in crowded fields. Here we use a probabilistic network of
#           information to perform the trajectory linking. This method uses
#           positional and visual information for track linking.
#
# Authors:  Alan R. Lowe (arl) a.lowe@ucl.ac.uk
#
# License:  See LICENSE.md
#
# Created:  14/08/2014
#-------------------------------------------------------------------------------

__author__ = "Alan R. Lowe"
__email__ = "a.lowe@ucl.ac.uk"

import logging
import hypothesis

from btrack.constants import Fates

from cvxopt.glpk import ilp
from cvxopt import matrix, spmatrix

# get the logger instance
logger = logging.getLogger('worker_process')

class TrackOptimiser(object):
    """ TrackOptimiser

    TrackOptimiser takes a list of tracklets, as producing by a tracking
    algorithm, such as BayesianTracker, and tries to resolve small linkage
    errors, finding a globally optimal solution.

    General types of linkage error to resolve:
        Track lost at periphery -> Terminate the track
        Track lost briefly, but same state -> Link tracklets together by merging
        Object splitting (eg cell division) -> Do not merge, but create graph

    The algorithm proceeds as follows:
        1. Calculate the hypotheses for each tracklet
        2. Solve the association problem/globally optimise
        3. Merge/'link' trajectories
        4. Assign a 'fate' or optimal hypothesis to each trajectory

    The global optimisation utilises integer optimisation from the GLPK library.
    Takes in the list of hypotheses and formulates as a mixed integer linear
    programming problem.  Then attempts to use GLPK to solve the association
    problem, returns a list of hypotheses to act upon.

    We set up a constraints matrix, A in this manner: (num_hypotheses x 2N)
    Uses cvxopt.gplk.ilp to:

    minimize    c'*x
    subject to  G*x <= h
                A*x = b
                x[I] are all integer (empty set in this case)
                x[B] are all binary

    Since this is a *minimisation* we need to invert the (log) probability of
    each hypothesis.

    Args:
        hypotheses: a list of PyHypothesis objects from the tracker

    Members:
        optimise()

    Returns:
        a list of selected hypotheses. These are indices into the list of
        PyHypothesis objects.

    Notes:
        If the hypotheses are badly formed, this can take *FOREVER*.

        'Report Automated Cell Lineage Construction' Al-Kofahi et al.
        Cell Cycle 2006 vol. 5 (3) pp. 327-335

        'Reliable cell tracking by global data association', Bise et al.
        2011 IEEE Symposium on Biomedical Imaging pp. 1004-1010

        'Local cellular neighbourhood controls proliferation in cell
        competition', Bove A, Gradeci D, Fujita Y, Banerjee S, Charras G and
        Lowe AR 2017 Mol. Biol. Cell vol 28 pp. 3215-3228
    """

    def __init__(self):
        self._hypotheses = []

    @property
    def hypotheses(self):
        return self._hypotheses
    @hypotheses.setter
    def hypotheses(self, hypotheses):
        self._hypotheses = hypotheses

    def optimise(self):
        """
        We set up a constraints matrix, A in this manner: (num_hypotheses x 2N).
        Rho is the log probability of accepting the hypothesis. x is the
        integer set of hypotheses selected.

        Since this is a *minimisation* we need to invert the (log) probability
        of each hypothesis.
        """

        logger.info('Setting up constraints matrix for global optimisation...')

        # anon function to renumber track ID from C++
        trk_idx = lambda h: int(h)-1

        # calculate the number of hypotheses, could use this moment to cull?
        n_hypotheses = len(self.hypotheses)
        N = max(set([int(h.ID) for h in self.hypotheses]))

        # A is the constraints matrix (store as sparse since mostly empty)
        # note that we make this in the already transposed form...
        A = spmatrix([], [], [], (2*N, n_hypotheses), 'd')
        rho = matrix(0., (n_hypotheses, 1), 'd')

        # iterate over the hypotheses and build the constraints
        # TODO(arl): vectorize this for increased performance
        for counter, h in enumerate(self.hypotheses):

            # set the hypothesis score
            rho[counter] = h.log_likelihood

            if h.type == Fates.FALSE_POSITIVE:
                # is this a false positive?
                trk = trk_idx(h.ID)
                A[trk,counter] = 1
                A[N+trk,counter] = 1
                continue

            elif h.type == Fates.INITIALIZE:
                # an initialisation, therefore we only present this in the
                # second half of the A matrix
                trk = trk_idx(h.ID)
                A[N+trk,counter] = 1
                continue

            elif h.type == Fates.TERMINATE:
                # a termination event, entry in first half only
                trk = trk_idx(h.ID)
                A[trk,counter] = 1
                continue

            elif h.type == Fates.APOPTOSIS:
                # an apoptosis event, entry in first half only
                trk = trk_idx(h.ID)
                A[trk,counter] = 1
                # A[N+trk,counter] = 1    # NOTE(arl): added 2019/08/29
                continue

            elif h.type == Fates.LINK:
                # a linkage event
                trk_i = trk_idx(h.ID)
                trk_j = trk_idx(h.link_ID)
                A[trk_i,counter] = 1
                A[N+trk_j,counter] = 1
                continue

            elif h.type == Fates.DIVIDE:
                # a branch event
                trk = trk_idx(h.ID)
                child_one = trk_idx(h.child_one_ID)
                child_two = trk_idx(h.child_two_ID)
                A[trk,counter] = 1
                A[N+child_one,counter] = 1
                A[N+child_two,counter] = 1
                continue

            elif h.type == Fates.MERGE:
                # a merge event
                trk = trk_idx(h.ID)
                parent_one = trk_idx(h.parent_one_ID)
                parent_two = trk_idx(h.parent_two_ID)
                A[N+trk,counter] = 1
                A[parent_one,counter] = 1
                A[parent_two,counter] = 1
                continue

            else:
                raise ValueError('Unknown hypothesis: {0:s}'.format(h.type))

        logger.info('Optimising...')

        # now set up the ILP solver
        G = spmatrix([], [], [], (2*N, n_hypotheses), 'd')
        h = matrix(0., (2*N,1), 'd')      # NOTE h cannot be a sparse matrix
        I = set()                         # empty set of x which are integer
        B = set(range(n_hypotheses))      # signifies all are binary in x
        b = matrix(1., (2*N,1), 'd')

        # now try to solve it!!!
        status, x = ilp(-rho, -G, h, A, b, I, B)

        # log the warning if not optimal solution
        if status != 'optimal':
            logger.warning('Optimizer returned status: {0:s}'.format(status))
            return []

        # return only the selected hypotheses
        results = [i for i,h in enumerate(self.hypotheses) if x[i]>0]

        logger.info('Optimisation complete. (Solution: {0:s})'.format(status))
        return results


if __name__ == '__main__':
    pass
