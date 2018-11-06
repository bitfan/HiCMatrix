import cooler
import logging
import numpy as np
from scipy.sparse import triu, csr_matrix
import pandas as pd
from past.builtins import zip
from builtins import super
log = logging.getLogger(__name__)
from .matrixFile import MatrixFile
from hicmatrix.utilities import toString
from hicmatrix.utilities import convertNansToOnes


class Cool(MatrixFile, object):

    def __init__(self, pMatrixFile=None):
        super().__init__(pMatrixFile)
        self.chrnameList = None
        self.correctionFactorTable = 'weight'
        self.correctionOperator = '*'
        self.enforceInteger = False

    def getInformationCoolerBinNames(self):
        return cooler.Cooler(self.matrixFileName).bins().columns.values

    def load(self, pApplyCorrection=None, pMatrixOnly=None):
        log.debug('Load in cool format')
        log.debug('self.chrnameList {}'.format(self.chrnameList))
        if self.matrixFileName is None:
            log.info('No matrix is initalized')
        if pApplyCorrection is None:
            pApplyCorrection = True
        try:
            cooler_file = cooler.Cooler(self.matrixFileName)
        except Exception:
            log.info("Could not open cooler file. Maybe the path is wrong or the given node is not available.")
            log.info('The following file was tried to open: {}'.format(self.matrixFileName))
            log.info("The following nodes are available: {}".format(cooler.io.ls(self.matrixFileName.split("::")[0])))
            exit()

        if self.chrnameList is None:
            matrixDataFrame = cooler_file.matrix(balance=False, sparse=True, as_pixels=True)
            used_dtype = np.int32
            if np.iinfo(np.int32).max < cooler_file.info['nbins']:
                used_dtype = np.int64
            data = np.empty(cooler_file.info['nnz'], dtype=used_dtype)
            instances = np.empty(cooler_file.info['nnz'], dtype=used_dtype)
            features = np.empty(cooler_file.info['nnz'], dtype=used_dtype)
            i = 0
            size = cooler_file.info['nbins'] // 32
            if size == 0:
                size = 1
            start_pos = 0
            while i < cooler_file.info['nbins']:
                csr_data = matrixDataFrame[i:i + size].values.astype(used_dtype).T
                lenght_data = len(csr_data[0])
                data[start_pos:start_pos + lenght_data] = csr_data[2]
                instances[start_pos:start_pos + lenght_data] = csr_data[0]
                features[start_pos:start_pos + lenght_data] = csr_data[1]
                start_pos += lenght_data
                del csr_data
                i += size
            matrix = csr_matrix((data, (instances, features)), shape=(cooler_file.info['nbins'], cooler_file.info['nbins']), dtype=used_dtype)
            del data
            del instances
            del features
        else:
            if len(self.chrnameList) == 1:
                try:
                    matrix = cooler_file.matrix(balance=False, sparse=True).fetch(self.chrnameList[0]).tocsr()
                except ValueError:
                    exit("Wrong chromosome format. Please check UCSC / ensembl notation.")
            else:
                exit("Operation to load more as one region is not supported.")

        cut_intervals_data_frame = None
        correction_factors_data_frame = None

        if self.chrnameList is not None:
            if len(self.chrnameList) == 1:
                cut_intervals_data_frame = cooler_file.bins().fetch(self.chrnameList[0])

                if self.correctionFactorTable in cut_intervals_data_frame:
                    correction_factors_data_frame = cut_intervals_data_frame[self.correctionFactorTable]
            else:
                exit("Operation to load more than one chr from bins is not supported.")
        else:
            if pApplyCorrection and self.correctionFactorTable in cooler_file.bins():
                correction_factors_data_frame = cooler_file.bins()[[self.correctionFactorTable]][:]

            cut_intervals_data_frame = cooler_file.bins()[['chrom', 'start', 'end']][:]

        correction_factors = None

        if correction_factors_data_frame is not None and pApplyCorrection:
            log.debug("Apply correction factors")
            # apply correction factors to matrix
            # a_i,j = a_i,j * c_i *c_j
            matrix.eliminate_zeros()
            matrix.data = matrix.data.astype(float)

            correction_factors = convertNansToOnes(np.array(correction_factors_data_frame.values).flatten())
            # apply only if there are not only 1's
            if np.sum(correction_factors) != len(correction_factors):
                instances, features = matrix.nonzero()
                instances_factors = correction_factors[instances]
                features_factors = correction_factors[features]
                instances_factors *= features_factors

                if self.correctionOperator == '*':
                    matrix.data *= instances_factors
                elif self.correctionOperator == '/':
                    matrix.data /= instances_factors

        cut_intervals = []

        for values in cut_intervals_data_frame.values:
            cut_intervals.append(tuple([toString(values[0]), values[1], values[2], 1.0]))

        # try to restore nan_bins.
        try:
            shape = matrix.shape[0] if matrix.shape[0] < matrix.shape[1] else matrix.shape[1]
            nan_bins = np.array(range(shape))
            nan_bins = np.setxor1d(nan_bins, matrix.indices)

            i = 0
            while i < len(nan_bins):
                if nan_bins[i] >= shape:
                    break
                i += 1
            nan_bins = nan_bins[:i]

        except Exception:
            nan_bins = None

        distance_counts = None

        return matrix, cut_intervals, nan_bins, distance_counts, correction_factors

    def save(self, pFileName, pSymmetric=True, pApplyCorrection=True):
        log.debug('Save in cool format')

        self.matrix.eliminate_zeros()
        if self.nan_bins is not None and len(self.nan_bins) > 0:
            # remove nan_bins by multipling them with 0 to set them to 0.
            correction_factors = np.ones(self.matrix.shape[0])
            correction_factors[self.nan_bins] = 0
            _instances, _features = self.matrix.nonzero()
            instances_factors = correction_factors[_instances]
            features_factors = correction_factors[_features]
            instances_factors *= features_factors
            self.matrix.data = self.matrix.data.astype(float)
            self.matrix.data *= instances_factors

        # set possible nans in data to 0
        self.matrix.data[np.argwhere(np.isnan(self.matrix.data))] = 0
        self.matrix.eliminate_zeros()

        # save only the upper triangle of the
        if pSymmetric:
            # symmetric matrix
            self.matrix = triu(self.matrix, format='csr')
        else:
            self.matrix = self.matrix

        self.matrix.eliminate_zeros()

        # create data frame for bins
        # self.cut_intervals is having 4 tuples, bin_data_frame should have 3.correction_factors
        # it looks like it is faster to create it with 4, and drop the last one
        # instead of handling this before.
        bins_data_frame = pd.DataFrame(self.cut_intervals, columns=['chrom', 'start', 'end', 'interactions']).drop('interactions', axis=1)

        if self.correction_factors is not None and pApplyCorrection:
            weight = convertNansToOnes(np.array(self.correction_factors).flatten())
            # self.correctionFactorTable
            bins_data_frame = bins_data_frame.assign(weight=weight)

        # get only the upper triangle of the matrix to save to disk
        # upper_triangle = triu(self.matrix, k=0, format='csr')
        # create a tuple list and use it to create a data frame

        # save correction factors and original matrix

        # revert correction to store orginal matrix
        if self.correction_factors is not None and pApplyCorrection:

            log.info("Reverting correction factors on matrix...")
            instances, features = self.matrix.nonzero()
            self.correction_factors = np.array(self.correction_factors)

            # do not apply if correction factors are just 1's
            if np.sum(self.correction_factors) != len(self.correction_factors):
                instances_factors = self.correction_factors[instances]
                features_factors = self.correction_factors[features]

                instances_factors *= features_factors
                self.matrix.data = self.matrix.data.astype(float)

                # Apply the invert operation to get the original data
                if self.correctionOperator == '*':
                    self.matrix.data /= instances_factors
                elif self.correctionOperator == '/':
                    self.matrix.data *= instances_factors

                instances_factors = None
                features_factors = None

                self.matrix.data = np.rint(self.matrix.data)
                self.matrix.data = self.matrix.data.astype(int)

        instances, features = self.matrix.nonzero()

        matrix_data_frame = pd.DataFrame(instances, columns=['bin1_id'], dtype=np.int32)
        del instances
        matrix_data_frame = matrix_data_frame.assign(bin2_id=features)
        del features

        if self.enforceInteger:
            cooler._writer.COUNT_DTYPE = np.int32
            data = np.rint(self.matrix.data)
            matrix_data_frame = matrix_data_frame.assign(count=data)
        else:
            matrix_data_frame = matrix_data_frame.assign(count=self.matrix.data)

        if self.matrix.dtype not in [np.int32, int]:
            log.warning("Writing non-standard cooler matrix. Datatype of matrix['count'] is: {}".format(self.matrix.dtype))
            cooler._writer.COUNT_DTYPE = self.matrix.dtype
        split_factor = 1
        if len(self.matrix.data) > 1e6:
            split_factor = 1e4
        cooler.io.create(cool_uri=pFileName,
                         bins=bins_data_frame,
                         pixels=np.array_split(matrix_data_frame, split_factor),
                         append=False)
