from mpl_toolkits.axes_grid.inset_locator import inset_axes
# Import batman, for lightcurve models:
import batman
# Import radvel, for RV models:
import radvel
# Import george for detrending:
import george
# Import celerite for detrending:
import celerite
# Plotting functions:
import seaborn as sns
import argparse
import matplotlib
import matplotlib.pyplot as plt

# Import dynesty for dynamic nested sampling:
import dynesty
# Import multinest for (importance) nested sampling:
import pymultinest
import sys

from scipy.interpolate import interp1d
import numpy as np
import utils
import os
import time

# Prepare the celerite term:
import celerite
from celerite import terms

class RotationTerm(terms.Term):
    parameter_names = ("log_amp", "log_timescale", "log_period", "log_factor")

    def get_real_coefficients(self, params):
        log_amp, log_timescale, log_period, log_factor = params
        f = np.exp(log_factor)
        return (
            np.exp(log_amp) * (1.0 + f) / (2.0 + f), 
            np.exp(-log_timescale),
        )   

    def get_complex_coefficients(self, params):
        log_amp, log_timescale, log_period, log_factor = params
        f = np.exp(log_factor)
        return (
            np.exp(log_amp) / (2.0 + f), 
            0.0,
            np.exp(-log_timescale),
            2*np.pi*np.exp(-log_period),
        )   

# Definition of user-defined arguments:
parser = argparse.ArgumentParser()
# This reads the lightcurve file. First column is time, second column is flux, third flux_err, fourth telescope name:
parser.add_argument('-lcfile', default=None)
# This reads the RV data. First column is time, second column is rv, third rv_err, fourth telescope name:
parser.add_argument('-rvfile', default=None)
# This reads the external parameters to fit to the photometry GP:
parser.add_argument('-lceparamfile', default=None)
# This reads the external parameters to fit to the RV GP:
parser.add_argument('-rveparamfile', default=None)
# This reads an output folder:
parser.add_argument('-ofolder', default='results')
# This defines the limb-darkening to be used. Can be either common to all instruments (e.g., give 'quadratic' as input), 
# or it can be different for every instrument, in which case you must pass a comma separated list of instrument-ldlaw pair, e.g.
# 'TESS-quadratic,CHAT-linear', etc.:
parser.add_argument('-ldlaw', default='quadratic')
# Lightcurve time definitions (e.g., 'TESS-TDB,CHAT-UTC', etc.). If not given, it is assumed all lightcurves are in TDB:
parser.add_argument('-lctimedef', default='TDB')
# Radial-velocities time definitions (e.g., 'HARPS-TDB,CORALIE-UTC', etc.). If not given, it is assumed all RVs are in UTC:
parser.add_argument('-rvtimedef', default='UTC')
# This reads the prior file:
parser.add_argument('-priorfile', default=None)
# This defines if rv units are m/s (ms) or km/s (kms); useful for plotting. Default is m/s:
parser.add_argument('-rvunits', default='ms')
# Define stellar density mean and stdev if you have it --- this will help with a constrained transit fit:
parser.add_argument('-sdensity_mean', default=None)
parser.add_argument('-sdensity_sigma', default=None)
# Define if the sampling for p and b in Espinoza (2018) wants to be used; define pl and pu (this assumes 
# sampling parameters in prior file are r1 and r2):
parser.add_argument('--efficient_bp', dest='efficient_bp', action='store_true')
parser.add_argument('-pl', default=None)
parser.add_argument('-pu', default=None)
# Number of live points:
parser.add_argument('-nlive', default=1000)
# Dealing with supersampling for long exposure times for LC. n_supersamp is the number of 
# supersampled points, exptime_supersamp the exposure time and instrument_supersamp the instrument
# for which you want to apply supersampling. If you need several instruments to have supersampling,
# you can give these input as comma separated values, e.g., '-instrument_supersamp TESS,K2 -n_supersamp 20,30 -exptime_supersamp 0.020434,0.020434' 
# will give values of n_supersamp of 20 and 30 to TESS and K2 lightcurves, respectively, and both of them with texp of 0.020434 days.
parser.add_argument('-n_supersamp', default=None)
parser.add_argument('-exptime_supersamp', default=None) 
parser.add_argument('-instrument_supersamp', default=None)
# Define if Dynamic Nested Sampling is to be used:
parser.add_argument('--dynamic', dest='dynamic', action='store_true')
# Define if dynesty will be used:
parser.add_argument('--use_dynesty', dest='use_dynesty', action='store_true')
# Define some arguments for dynesty runs (see https://dynesty.readthedocs.io/en/latest/api.html). First, bounded method for dynesty:
parser.add_argument('-dynesty_bound', default='multi')
# Method used to sample uniformly within the likelihood constraint, conditioned on the provided bounds:
parser.add_argument('-dynesty_sample', default='rwalk')
# Number of threads to use within dynesty (giving a number here assumes one wants to perform multithreading):
parser.add_argument('-dynesty_nthreads', default='none')

args = parser.parse_args()

# Check the dynesty and dynamic flag:
use_dynesty = args.use_dynesty
dynamic = args.dynamic

# Check dynesty options:
dynesty_bound = args.dynesty_bound
dynesty_sample = args.dynesty_sample
dynesty_nthreads = args.dynesty_nthreads

from dynesty.utils import resample_equal

print('\n\t \t ---------------------------------------------\n')

print('\t \t                   juliet v.1.0            ') 
print('\t \t                                     ')
print('\t \t      Authors: N. Espinoza, D. Kossakowski')
print('\t \t      Contact: espinoza at mpia.de\n')

print('\t \t ---------------------------------------------\n')
# If not ran already, run dynesty, save posterior samples and evidences to pickle file:
# For this, first check if dynamic or normal NS is going to be used:
if use_dynesty:
    if dynamic:
        prefix = 'dynamic_dynesty_'
        print('\t Running DYNAMIC NESTED SAMPLING (dynesty)')
    else:
        prefix = 'dynesty_'
        print('\t Running NESTED SAMPLING (dynesty)')
else:
    prefix = 'multinest_'
    print('\t Running NESTED SAMPLING (multinest)')

# Extract parameters for efficient sampling of b and p, calculate Ar:
efficient_bp = args.efficient_bp

if efficient_bp:
    print('\t Efficient sampling of the (b,p) plane detected')
    pl,pu = np.double(args.pl),np.double(args.pu)
    Ar = (pu - pl)/(2. + pl + pu)
    print('\t > pl:',pl,'pu:',pu,'Ar:',Ar)

# Output folder:
out_folder = args.ofolder+'/'
if not os.path.exists(out_folder):
    os.mkdir(out_folder)

# Read lightcurves and rvs --- save them to out_folder if not already there:
lcfilename = args.lcfile
rvfilename = args.rvfile
if (not os.path.exists(out_folder+'lc.dat')) and (lcfilename is not None):
    os.system('cp '+lcfilename+' '+out_folder+'lc.dat')
if (not os.path.exists(out_folder+'rvs.dat')) and (rvfilename is not None):
    os.system('cp '+rvfilename+' '+out_folder+'rvs.dat')

sd_mean,sd_sigma = args.sdensity_mean,args.sdensity_sigma
stellar_density = False
if sd_mean is not None:
    sd_mean,sd_sigma = np.double(sd_mean),np.double(sd_sigma)
    stellar_density = True

if lcfilename is not None:
    t_lc,f_lc,ferr_lc,instruments_lc,instrument_indexes_lc,\
    ninstruments_lc,inames_lc = utils.readlc(lcfilename)
    print('\t Photometric instruments:',inames_lc)

    # First of all, generate a dictionary for each instrument. This will save the 
    # lightcurve objects:
    lc_dictionary = {}
    for i in range(ninstruments_lc):
        lc_dictionary[inames_lc[i]] = {}
        lc_dictionary[inames_lc[i]]['resampling'] = False
        lc_dictionary[inames_lc[i]]['GPDetrend'] = False

    # Convert times from TDB to UTC to match RVs. First, see if there is more than one instrument 
    # time definition. If not, assume all times are TDB and thus convert all times to UTC:
    lctimedefs = args.lctimedef.split(',')
    if len(lctimedefs) == 1:
        t_lc = utils.convert_time(lctimedefs[0].split()[0].lower()+'->utc',t_lc)
    else:
        for lctimedef in lctimedefs:
            instrument,timedef = lctimedef.split('-')
            t_lc[instrument_indexes_lc[instrument]] = utils.convert_time(timedef.split()[0].lower()+'->utc',t_lc[instrument_indexes_lc[instrument]])
    # Extract limb-darkening law. If just one is given, assume same LD law for all instruments. If not, assume a 
    # different law for each instrument:
    ld_laws = args.ldlaw.split(',')
    if len(ld_laws) == 1:
        for i in range(ninstruments_lc):
            lc_dictionary[inames_lc[i]]['ldlaw'] = ld_laws[0].split()[0].lower()
    else:
        for ld_law in ld_laws:
            instrument,ld = ld_law.split('-')
            lc_dictionary[instrument.split()[0]]['ldlaw'] = ld.split()[0].lower()
                                   
    # Extract supersampling parameters for transit model. If not given for each instrument, assume all must be 
    # resampled:
    if args.instrument_supersamp is not None:
        instrument_ss = args.instrument_supersamp.split(',')
        n_ss = np.array(args.n_supersamp.split(',')).astype('int')
        exptime_ss = np.array(args.exptime_supersamp.split(',')).astype('double')
        for i in range(len(instrument_ss)):
            lc_dictionary[instrument_ss[i]]['resampling'] = True
            lc_dictionary[instrument_ss[i]]['nresampling'] = n_ss[i]
            lc_dictionary[instrument_ss[i]]['exptimeresampling'] = exptime_ss[i]
    else:
        if args.n_supersamp is not None:
            n_ss = int(args.n_supersamp)
        else: 
            n_ss = args.n_supersamp
        if args.exptime_supersamp is not None:
            exptime_ss = float(args.exptime_supersamp)
            for i in range(ninstruments_lc):
                lc_dictionary[inames_lc[i]]['resampling'] = True
                lc_dictionary[inames_lc[i]]['nresampling'] = n_ss
                lc_dictionary[inames_lc[i]]['exptimeresampling'] = exptime_ss
                
        else: 
            exptime_ss = args.exptime_supersamp
    ###################

else:
    inames_lc = []
if rvfilename is not None:
    t_rv,rv_rv,rverr_rv,instruments_rv,instrument_indexes_rv,\
    ninstruments_rv,inames_rv = utils.readlc(rvfilename)
    rvresiduals = np.zeros(len(t_rv))
    rvresiduals_err = np.zeros(len(t_rv))
    idx_ordered_rv = np.argsort(len(t_rv))
    
    # First of all, generate a dictionary for RVs. This will save the 
    # RV objects:
    rv_dictionary = {} 
    rv_dictionary['GPDetrend'] = False
    for i in range(ninstruments_rv):
        rv_dictionary[inames_rv[i]] = {} 
    #    lc_dictionary[inames_lc[i]]['resampling'] = False
    #    lc_dictionary[inames_lc[i]]['GPDetrend'] = False
else:
    inames_rv = []

# Read priors, number of planets that transit and number of planets in RVs:
priorfile = args.priorfile
priors,n_transit,n_rv,numbering_transit,numbering_rv,n_params = utils.readpriors(priorfile)

# Check if RV line will be fitted:
if 'rv_slope' in priors.keys():
    if 'rv_quad' in priors.keys():
        fitrvquad = True
        fitrvline = False
    else:
        fitrvline = True
        fitrvquad = False
else:
    fitrvline = False
    fitrvquad = False

# Check if stellar density will be fitted instead of the a/R_*:
if 'rho' in priors.keys():
    fitrho = True
    print('\t Fitting of stellar density detected.')
else:
    fitrho = False

# Check eccentricity parametrization for each planet in the juliet numbering scheme.
# 0 = ecc, omega  1: ecosomega,esinomega  2: sqrt(e)cosomega, sqrt(e)sinomega
ecc_parametrization = {} # np.zeros(np.max([n_transit,n_rv]))
ecc_parametrization['rv'] = {}
ecc_parametrization['transit'] = {}
# First for the transiting planets:
for n in range(n_transit):
    i = numbering_transit[n]
    if 'ecosomega_p'+str(i) in priors.keys():
        ecc_parametrization['transit'][i] = 1
        print('\t ecosomega,esinomega parametrization for transiting planet ',i)
    elif 'secosomega_p'+str(i) in priors.keys():
        ecc_parametrization['transit'][i] = 2
        print('\t sqrt(e)cosomega,sqrt(e)sinomega parametrization for transiting planet ',i)
    else:
        ecc_parametrization['transit'][i] = 0
        print('\t e,omega parametrization for transiting planet ',i)

for n in range(n_rv):
    i = numbering_rv[n]
    if 'ecosomega_p'+str(i) in priors.keys():
        ecc_parametrization['rv'][i] = 1
        print('\t ecosomega,esinomega parametrization for RV planet ',i)
    elif 'secosomega_p'+str(i) in priors.keys():
        ecc_parametrization['rv'][i] = 2
        print('\t sqrt(e)cosomega,sqrt(e)sinomega parametrization for RV planet ',i)
    else:
        ecc_parametrization['rv'][i] = 0
        print('\t e,omega parametrization for RV planet ',i)

# Save prior file to output folder if not already there:
if not os.path.exists(out_folder+'priors.dat'):
    os.system('cp '+priorfile+' '+out_folder+'priors.dat')

# RV units:
rvunits = args.rvunits

print('\t Fitting ',n_transit,' transiting planets and ',n_rv,' radial-velocity systems.')
print('\t ',n_params,' free parameters.')
if fitrvline:
    print('\t RV Line Fit: True')
if fitrvquad:
    print('\t RV Quadratic Fit: True')

if lcfilename is not None:
    # Float the times (batman doesn't like non-float 64):
    t_lc = t_lc.astype('float64')

# Extract external parameter file for LC GP:
lceparamfile = args.lceparamfile
if lceparamfile is not None:
    GPDict = utils.readeparams(lceparamfile)
    for instrument in GPDict.keys():
        print('\t Fitting photometric GP to ',instrument,' instrument.')
        # Detect the GP the user wants for this instrument using the priors dictionary. 
        # For this, consider the possibility that the user might want to combine the GP parameters 
        # between instruments, so iterate.
        for pnames in priors.keys():
            vec = pnames.split('_')
            if (vec[0] == 'GP') and ('alpha0' in vec[1]) and (instrument in vec):
                # Detected multi-dimensional squared-exponential GP:
                lc_dictionary[instrument]['GPType'] = 'SEKernel'
                break
            if (vec[0] == 'GP') and ('Gamma' in vec[1]) and (instrument in vec):
                # Detected exp-sine-squared kernel:
                lc_dictionary[instrument]['GPType'] = 'ExpSineSquaredSEKernel'
                break
            if (vec[0] == 'GP') and ('B' in vec[1]) and (instrument in vec):
                # Detected celerite quasi-periodic kernel:
                lc_dictionary[instrument]['GPType'] = 'CeleriteQPKernel'
                break
        print('\t Detected ',lc_dictionary[instrument]['GPType'],'for the GP')
        # For each instrument for which there are external parameters, activate GP:
        lc_dictionary[instrument]['GPDetrend'] = True 
        # Save variables, standarize them if GP is SEKernel:
        lc_dictionary[instrument]['X'] = GPDict[instrument]['variables']
        lc_dictionary[instrument]['nX'] = lc_dictionary[instrument]['X'].shape[1]
        print('\t (',lc_dictionary[instrument]['nX'],'external parameters)')
        if lc_dictionary[instrument]['GPType'] == 'SEKernel':
            for i in range(lc_dictionary[instrument]['nX']):
                lc_dictionary[instrument]['X'][:,i] = (lc_dictionary[instrument]['X'][:,i] - \
                                                      np.mean(lc_dictionary[instrument]['X'][:,i]))/\
                                                      np.sqrt(np.var(lc_dictionary[instrument]['X'][:,i]))
        if lc_dictionary[instrument]['GPType'] == 'CeleriteQPKernel':
            rot_kernel = terms.TermSum(RotationTerm(log_amp=np.log(10.),\
                                                    log_timescale=np.log(10.0),\
                                                    log_period=np.log(3.0),\
                                                    log_factor=np.log(1.0)))

            # Now that we know the type of GP, we extract the "instrument" corresponding to each GP 
            # parameter. For example, for the ExpSineSquaredKernel, it might happen the user wants 
            # to have a common GP_Prot parameter shared along many instruments, e.g., GP_Prot_TESS_K2_RV,
            # which means the user wants a common Prot for TESS and K2 photometry, and also for the RVs. However, 
            # the same GP might have a different Gamma, i.e., there might be a GP_Gamma_TESS, GP_Gamma_K2 and GP_Gamma_RV.
            # The idea here is to, e.g., in the case of TESS photometry, gather lc_dictionary[instrument]['GP_Prot'] = 
            # 'TESS_K2_RV', and lc_dictionary[instrument]['GP_Gamma'] = 'TESS':
            for GPvariable in ['B','C','L','Prot']:
                for pnames in priors.keys():
                    vec = pnames.split('_')
                    if (vec[0] == 'GP') and (GPvariable in vec[1]) and (instrument in vec):
                        lc_dictionary[instrument]['GP_'+GPvariable] = '_'.join(vec[2:])
         
            # Jitter term:
            kernel_jitter = terms.JitterTerm(np.log(100*1e-6))

            # Wrap GP object to compute likelihood:
            kernel = rot_kernel + kernel_jitter
            lc_dictionary[instrument]['GPObject'] = celerite.GP(kernel, mean=0.0)
            # Note order of GP Vector: logB, logL, logProt, logC, logJitter
            lc_dictionary[instrument]['GPVector'] = np.zeros(5)
            lc_dictionary[instrument]['GPObject'].compute(lc_dictionary[instrument]['X'],yerr=ferr_lc[instrument_indexes_lc[instrument]])          

        if lc_dictionary[instrument]['GPType'] == 'ExpSineSquaredSEKernel':
            for GPvariable in ['sigma','alpha','Gamma','Prot']:
                for pnames in priors.keys():
                    vec = pnames.split('_')
                    if (vec[0] == 'GP') and (GPvariable in vec[1]) and (instrument in vec):
                        lc_dictionary[instrument]['GP_'+GPvariable] = '_'.join(vec[2:])

            # Generate GP Base Kernel (Constant * ExpSquared * ExpSine2):
            K1 = 1.*george.kernels.ExpSquaredKernel(metric = 1.0)
            K2 = george.kernels.ExpSine2Kernel(gamma=1.0,log_period=1.0)
            lc_dictionary[instrument]['GPKernelBase'] = K1*K2
            # Generate Jitter term:
            lc_dictionary[instrument]['GPKernelJitter'] = george.modeling.ConstantModel(np.log((200.*1e-6)**2.))

            # Generate full kernel (i.e., GP plus jitter), generating full GP object:
            lc_dictionary[instrument]['GPObject'] = george.GP(lc_dictionary[instrument]['GPKernelBase'], mean=0.0,fit_mean=False,\
                                                    white_noise=lc_dictionary[instrument]['GPKernelJitter'],\
                                                    fit_white_noise=True,solver=george.HODLRSolver, seed=42)
            # Create the parameter vector --- note its dim: GP_sigma (+1) + GP_alpha (+1) + GP_Gamma (+1) + GP_Prot (+1) + Jitter term (+1): 5.
            # Given how we defined the vector, first parameter of vector is jitter, second log_(GP_sigma**2), third 1./(2*alpha), fourth Gamma and fifth logProt.
            lc_dictionary[instrument]['GPVector'] = np.zeros(5)
            # Finally, compute GP object. Note we add the lightcurve uncertainties here, which are added in quadrature to the 
            # diagonal terms in the covariance matrix by george internally:
            lc_dictionary[instrument]['GPObject'].compute(lc_dictionary[instrument]['X'],yerr=ferr_lc[instrument_indexes_lc[instrument]])

        if lc_dictionary[instrument]['GPType'] == 'SEKernel':
            GPvariables = ['sigma']
            for ialpha in range(lc_dictionary[instrument]['nX']):
                GPvariables = GPvariables + ['alpha'+str(ialpha)]
            for GPvariable in GPvariables:
                for pnames in priors.keys():
                    vec = pnames.split('_')
                    if (vec[0] == 'GP') and (GPvariable in vec[1]) and (instrument in vec):
                        lc_dictionary[instrument]['GP_'+GPvariable] = '_'.join(vec[2:])

            # Generate GPExpSquared Base Kernel:
            lc_dictionary[instrument]['GPKernelBase'] = 1.*george.kernels.ExpSquaredKernel(np.ones(lc_dictionary[instrument]['nX']),\
                                                        ndim=lc_dictionary[instrument]['nX'],\
                                                        axes=range(lc_dictionary[instrument]['nX']))
            # Generate Jitter term:
            lc_dictionary[instrument]['GPKernelJitter'] = george.modeling.ConstantModel(np.log((200.*1e-6)**2.))

            # Generate full kernel (i.e., GPExpSquared plus jitter), generating full GP object:
            lc_dictionary[instrument]['GPObject'] = george.GP(lc_dictionary[instrument]['GPKernelBase'], mean=0.0,fit_mean=False,\
                                                    white_noise=lc_dictionary[instrument]['GPKernelJitter'],\
                                                    fit_white_noise=True)
            # Create the parameter vector --- note it equals number of external parameters plus 2: amplitude of the GP 
            # component and jitter:
            lc_dictionary[instrument]['GPVector'] = np.zeros(lc_dictionary[instrument]['X'].shape[1] + 2)
            # Finally, compute GP object. Note we add the lightcurve uncertainties here, which are added in quadrature to the 
            # diagonal terms in the covariance matrix by george internally:
            lc_dictionary[instrument]['GPObject'].compute(lc_dictionary[instrument]['X'],yerr=ferr_lc[instrument_indexes_lc[instrument]])

# Extract external parameter file for RV GP:
rveparamfile = args.rveparamfile
if rveparamfile is not None:
    GPDict = utils.readeparams(rveparamfile,RV=True)
    print('\t Fitting RV GP.')
    rv_dictionary['GPDetrend'] = True
    # Detect the GP the user wants for the RVs using the priors dictionary. 
    # For this, consider the possibility that the user might want to combine the GP parameters 
    # between instruments, so iterate.
    for pnames in priors.keys():
        vec = pnames.split('_')
        if (vec[0] == 'GP') and ('alpha0' in vec[1]) and ('rv' in vec[-1].lower()):
            # Detected multi-dimensional squared-exponential GP:
            rv_dictionary['GPType'] = 'SEKernel'
            break
        if (vec[0] == 'GP') and ('Gamma' in vec[1]) and ('rv' in vec[-1].lower()):
            # Detected exp-sine-squared kernel:
            rv_dictionary['GPType'] = 'ExpSineSquaredSEKernel'
            break
        if (vec[0] == 'GP') and ('B' in vec[1]) and ('rv' in vec[-1].lower()):
            # Detected celerite quasi-periodic kernel:
            rv_dictionary['GPType'] = 'CeleriteQPKernel'
            break
    print('\t Detected ',rv_dictionary['GPType'],'for the GP')
    # Save variables, standarize them if GP is SEKernel:
    rv_dictionary['X'] = GPDict['variables']
    rv_dictionary['nX'] = rv_dictionary['X'].shape[1]
    print('\t (',rv_dictionary['nX'],'external parameters)')

    if rv_dictionary['GPType'] == 'SEKernel':
        for i in range(rv_dictionary['nX']):
            rv_dictionary['X'][:,i] = (rv_dictionary['X'][:,i] - \
                                       np.mean(rv_dictionary['X'][:,i]))/\
                                       np.sqrt(np.var(rv_dictionary['X'][:,i]))
        print('\t Not yet supported SEKernel for RVs (do you really need it?).')
        import sys
        sys.exit()

    if rv_dictionary['GPType'] == 'CeleriteQPKernel':
        # Now that we know the type of GP, we extract the "instrument" corresponding to each GP 
        # parameter. For example, for the ExpSineSquaredKernel, it might happen the user wants 
        # to have a common GP_Prot parameter shared along many instruments, e.g., GP_Prot_TESS_K2_RV,
        # which means the user wants a common Prot for TESS and K2 photometry, and also for the RVs. However, 
        # the same GP might have a different Gamma, i.e., there might be a GP_Gamma_TESS, GP_Gamma_K2 and GP_Gamma_RV.
        # The idea here is to, e.g., in the case of TESS photometry, gather lc_dictionary[instrument]['GP_Prot'] = 
        # 'TESS_K2_RV', and lc_dictionary[instrument]['GP_Gamma'] = 'TESS':
        for GPvariable in ['B','C','L','Prot']:
            for pnames in priors.keys():
                vec = pnames.split('_')
                if (vec[0] == 'GP') and (GPvariable in vec[1]) and ('rv' in vec[-1].lower()):
                    rv_dictionary['GP_'+GPvariable] = '_'.join(vec[2:])

        #for instrument in inames_rv:
        rot_kernel = terms.TermSum(RotationTerm(log_amp=np.log(10.),\
                                                log_timescale=np.log(10.0),\
                                                log_period=np.log(3.0),\
                                                log_factor=np.log(1.0)))
        # Jitter term; dont add it, jitters will be added directly on the log-like (see Espinoza+2018).
        #kernel_jitter = terms.JitterTerm(np.log(100*1e-6))

        # Wrap GP object to compute likelihood:
        kernel = rot_kernel #+ kernel_jitter
        rv_dictionary['GPObject'] = celerite.GP(kernel, mean=0.0)
        # Note order of GP Vector: logB, logL, logProt, logC, logJitter
        rv_dictionary['GPVector'] = np.zeros(5)
        rv_dictionary['GPObject'].compute(rv_dictionary['X'],yerr=rverr_rv)

    if rv_dictionary['GPType'] == 'ExpSineSquaredSEKernel':
        for GPvariable in ['sigma','alpha','Gamma','Prot']:
            for pnames in priors.keys():
                vec = pnames.split('_')
                if (vec[0] == 'GP') and (GPvariable in vec[1]) and ('rv' in vec[-1].lower()):
                    rv_dictionary['GP_'+GPvariable] = '_'.join(vec[2:])

        #for instrument in inames_rv:
        # Generate GP Base Kernel (Constant * ExpSquared * ExpSine2):
        K1 = 1.*george.kernels.ExpSquaredKernel(metric = 1.0)
        K2 = george.kernels.ExpSine2Kernel(gamma=1.0,log_period=1.0)

        # Generate kernel part:
        rv_dictionary['GPKernelBase'] = K1*K2
        # Generate Jitter term:
        #rv_dictionary['GPKernelJitter'] = george.modeling.ConstantModel(np.log((200.*1e-6)**2.))

        # Generate full kernel (i.e., GP plus jitter), generating full GP object:
        rv_dictionary['GPObject'] = george.GP(rv_dictionary['GPKernelBase'], mean=0.0,fit_mean=False,\
                                                          fit_white_noise=False)#,solver=george.HODLRSolver, seed=42)
        # Create the parameter vector --- note its dim: GP_sigma (+1) + GP_alpha (+1) + GP_Gamma (+1) + GP_Prot (+1): 4.
        # Given how we defined the vector, first parameter of vector log_(GP_sigma**2), 2 1./(2*alpha), 3 Gamma and 4 logProt.
        rv_dictionary['GPVector'] = np.zeros(4)
        # Finally, compute GP object. 
        rv_dictionary['GPObject'].compute(rv_dictionary['X'],yerr=rverr_rv)

# Other inputs like, e.g., nlive points:
n_live_points = int(args.nlive)

# Define transit-related functions:
def reverse_ld_coeffs(ld_law, q1, q2):
    if ld_law == 'quadratic':
        coeff1 = 2.*np.sqrt(q1)*q2
        coeff2 = np.sqrt(q1)*(1.-2.*q2)
    elif ld_law=='squareroot':
        coeff1 = np.sqrt(q1)*(1.-2.*q2)
        coeff2 = 2.*np.sqrt(q1)*q2
    elif ld_law=='logarithmic':
        coeff1 = 1.-np.sqrt(q1)*q2
        coeff2 = 1.-np.sqrt(q1)
    elif ld_law == 'linear':
        return q1,q2
    return coeff1,coeff2

def init_batman(t,law, n_ss=None, exptime_ss=None):
    """  
    This function initializes the batman code.
    """
    params = batman.TransitParams()
    params.t0 = 0.
    params.per = 1.
    params.rp = 0.1
    params.a = 15.
    params.inc = 87.
    params.ecc = 0.
    params.w = 90.
    if law == 'linear':
        params.u = [0.5]
    else:
        params.u = [0.1,0.3]
    params.limb_dark = law
    if n_ss is None or exptime_ss is None:
        m = batman.TransitModel(params, t)
    else: 
        m = batman.TransitModel(params, t, supersample_factor=n_ss, exp_time=exptime_ss)
    return params,m

def get_transit_model(t,t0,P,p,a,inc,q1,q2,ld_law,n_ss,exptime_ss):
    params,m = init_batman(t,law=ld_law,n_ss=n_ss,exptime_ss=exptime_ss)
    coeff1,coeff2 = reverse_ld_coeffs(ld_law, q1, q2)
    params.t0 = t0
    params.per = P
    params.rp = p
    params.a = a
    params.inc = inc
    if ld_law == 'linear':
        params.u = [coeff1]
    else:
        params.u = [coeff1,coeff2]
    return m.light_curve(params)

def init_radvel(nplanets=1):
    return radvel.model.Parameters(nplanets,basis='per tc e w k')

def transform_prior(val,pinfo):
    if pinfo['type'] == 'uniform':
        return utils.transform_uniform(val,pinfo['value'][0],pinfo['value'][1])
    if pinfo['type'] == 'normal':
        return utils.transform_normal(val,pinfo['value'][0],pinfo['value'][1])
    if pinfo['type'] == 'jeffreys':
        return utils.transform_loguniform(val,pinfo['value'][0],pinfo['value'][1])
    if pinfo['type'] == 'beta':
        return utils.transform_beta(val,pinfo['value'][0],pinfo['value'][1])
    if pinfo['type'] == 'exponential':
        return utils.transform_exponential(val)

if lcfilename is not None:
    # Initialize batman for each different lightcurve:
    for instrument in lc_dictionary.keys():
        if lc_dictionary[instrument]['resampling']:
            lc_dictionary[instrument]['params'],lc_dictionary[instrument]['m'] = init_batman(t_lc[instrument_indexes_lc[instrument]], \
                                                           law=lc_dictionary[instrument]['ldlaw'], \
                                                           n_ss=lc_dictionary[instrument]['nresampling'],\
                                                           exptime_ss=lc_dictionary[instrument]['exptimeresampling'])  
        else:
            lc_dictionary[instrument]['params'],lc_dictionary[instrument]['m'] = init_batman(t_lc[instrument_indexes_lc[instrument]], \
                                                           law=lc_dictionary[instrument]['ldlaw'])

if rvfilename is not None:
    # Initialize radvel:
    radvel_params = init_radvel(nplanets=n_rv)

# Define gaussian log-likelihood:
log2pi = np.log(2.*np.pi)
def gaussian_log_likelihood(residuals,errors,jitter):
    taus = 1./(errors**2 + jitter**2)
    return -0.5*(len(residuals)*log2pi+np.sum(np.log(1./taus)+taus*(residuals**2)))

# Now define  priors and log-likelihood. Note here the idea is that any fixed parameters don't 
# receive prior numbering. 
transformed_priors = np.zeros(n_params)
def prior(cube, ndim=None, nparams=None):
    pcounter = 0
    for pname in priors.keys(): 
         if priors[pname]['type'] != 'fixed':
             if use_dynesty:
                 transformed_priors[pcounter] = transform_prior(cube[pcounter],priors[pname])
             else:
                 cube[pcounter] = transform_prior(cube[pcounter],priors[pname])
             pcounter += 1
    if use_dynesty:
        return transformed_priors
        

if lcfilename is not None:
    lcones = np.ones(len(t_lc))

# Gravitational constant (for stellar density):
G = 6.67408e-11 # mks

# Maximum eccentricity limit:
ecclim = 0.95

def loglike(cube, ndim=None, nparams=None):
    # Evaluate the log-likelihood. For this, first extract all inputs:
    pcounter = 0
    for pname in priors.keys():
        if priors[pname]['type'] != 'fixed':
            priors[pname]['cvalue'] = cube[pcounter]
            pcounter += 1

    # Photometric terms first:
    if lcfilename is not None:
        # Before everything continues, make sure periods are chronologically ordered (this is to avoid multiple modes due to 
        # periods "jumping" between planet numbering):
        for n in range(n_transit):
            i = numbering_transit[n]
            if n == 0:
                cP = priors['P_p'+str(i)]['cvalue']
            else:
                if cP < priors['P_p'+str(i)]['cvalue']:
                    cP = priors['P_p'+str(i)]['cvalue']
                else:
                    return -1e101
            if cP < 0.:
                return -1e101
        # Generate lightcurve models for each instrument:
        lcmodel = np.copy(lcones)
        for instrument in inames_lc:
            # For each transit model iterate through the 
            # number of planets, multiplying their transit models:
            for n in range(n_transit):
                i = numbering_transit[n]
                if lc_dictionary[instrument]['ldlaw'] != 'linear':
                    coeff1,coeff2 = reverse_ld_coeffs(lc_dictionary[instrument]['ldlaw'],priors['q1_'+instrument]['cvalue'],\
                                    priors['q2_'+instrument]['cvalue'])
                    lc_dictionary[instrument]['params'].u = [coeff1,coeff2]
                else:
                    lc_dictionary[instrument]['params'].u = [priors['q1_'+instrument]['cvalue']]

                if efficient_bp:
                    if not fitrho:
                        a,r1,r2,t0,P = priors['a_p'+str(i)]['cvalue'],priors['r1_p'+str(i)]['cvalue'],\
                                       priors['r2_p'+str(i)]['cvalue'], priors['t0_p'+str(i)]['cvalue'], \
                                       priors['P_p'+str(i)]['cvalue']
                    else:
                        rho,r1,r2,t0,P = priors['rho']['cvalue'],priors['r1_p'+str(i)]['cvalue'],\
                                         priors['r2_p'+str(i)]['cvalue'], priors['t0_p'+str(i)]['cvalue'], \
                                         priors['P_p'+str(i)]['cvalue']
                        a = ((rho*G*((P*24.*3600.)**2))/(3.*np.pi))**(1./3.)
                    if r1 > Ar:
                        b,p = (1+pl)*(1. + (r1-1.)/(1.-Ar)),\
                              (1-r2)*pl + r2*pu
                    else:
                        b,p = (1. + pl) + np.sqrt(r1/Ar)*r2*(pu-pl),\
                              pu + (pl-pu)*np.sqrt(r1/Ar)*(1.-r2)
                else:
                    if not fitrho:
                        a,b,p,t0,P = priors['a_p'+str(i)]['cvalue'],priors['b_p'+str(i)]['cvalue'],\
                                     priors['p_p'+str(i)]['cvalue'], priors['t0_p'+str(i)]['cvalue'], \
                                     priors['P_p'+str(i)]['cvalue']
                    else:
                        rho,b,p,t0,P = priors['rho']['cvalue'],priors['b_p'+str(i)]['cvalue'],\
                                     priors['p_p'+str(i)]['cvalue'], priors['t0_p'+str(i)]['cvalue'], \
                                     priors['P_p'+str(i)]['cvalue']
                        a = ((rho*G*((P*24.*3600.)**2))/(3.*np.pi))**(1./3.)
                if ecc_parametrization['transit'][i] == 0:
                    ecc,omega = priors['ecc_p'+str(i)]['cvalue'],priors['omega_p'+str(i)]['cvalue']
                elif ecc_parametrization['transit'][i] == 1:
                    ecc = np.sqrt(priors['ecosomega_p'+str(i)]['cvalue']**2+priors['esinomega_p'+str(i)]['cvalue']**2)
                    omega = np.arctan2(priors['esinomega_p'+str(i)]['cvalue'],priors['ecosomega_p'+str(i)]['cvalue'])*180./np.pi
                else:
                    ecc = priors['secosomega_p'+str(i)]['cvalue']**2+priors['sesinomega_p'+str(i)]['cvalue']**2
                    omega = np.arctan2(priors['sesinomega_p'+str(i)]['cvalue'],priors['secosomega_p'+str(i)]['cvalue'])*180./np.pi

                if ecc>ecclim:
                    return -1e101
                else:
                    ecc_factor = (1. + ecc*np.sin(omega * np.pi/180.))/(1. - ecc**2)
                    inc_inv_factor = (b/a)*ecc_factor
                    if not (b>1.+p or inc_inv_factor >=1.):
                        inc = np.arccos(inc_inv_factor)*180./np.pi
                        lc_dictionary[instrument]['params'].t0 = t0 
                        lc_dictionary[instrument]['params'].per = P
                        lc_dictionary[instrument]['params'].rp = p
                        lc_dictionary[instrument]['params'].a = a
                        lc_dictionary[instrument]['params'].inc = inc
                        lc_dictionary[instrument]['params'].ecc = ecc
                        lc_dictionary[instrument]['params'].w = omega
                        lcmodel[instrument_indexes_lc[instrument]] = lcmodel[instrument_indexes_lc[instrument]]*lc_dictionary[instrument]['m'].light_curve(lc_dictionary[instrument]['params'])
                        #iidx = np.where(lcmodel!=1.)[0]
                    else:
                        return -1e101 
    # Compute combined log-likelihood for lightcurve data:
    log_likelihood = 0.0
    for instrument in inames_lc:
        inst_model = lcmodel[instrument_indexes_lc[instrument]]*priors['mdilution_'+instrument]['cvalue'] \
                     + priors['mflux_'+instrument]['cvalue']  
        residuals = f_lc[instrument_indexes_lc[instrument]] - inst_model

        # If not GP Detrend (which means no external parameters given for the instrument), 
        # return gaussian log-likelihood:
        if not lc_dictionary[instrument]['GPDetrend']:
            log_likelihood += gaussian_log_likelihood(residuals,\
                                                      ferr_lc[instrument_indexes_lc[instrument]],\
                                                      priors['sigma_w_'+instrument]['cvalue']*1e-6)
        else:
            if lc_dictionary[instrument]['GPType'] == 'SEKernel':
                # Save the log(variance) of the jitter term on the current GP vector:
                lc_dictionary[instrument]['GPVector'][0] = np.log((priors['sigma_w_'+instrument]['cvalue']*1e-6)**2.)
                # Save pooled variance of the GP process:
                lc_dictionary[instrument]['GPVector'][1] = np.log((priors['GP_sigma_'+lc_dictionary[instrument]['GP_sigma']]['cvalue']*1e-6)**2.)
                # Now save (log of) coefficients of each GP term:
                for i in range(lc_dictionary[instrument]['nX']):
                    lc_dictionary[instrument]['GPVector'][2+i] = np.log(1./priors['GP_alpha'+str(i)+'_'+lc_dictionary[instrument]['GP_alpha'+str(i)]]['cvalue'])
            if lc_dictionary[instrument]['GPType'] == 'ExpSineSquaredSEKernel':
                # Save the log(variance) of the jitter term on the current GP vector:
                lc_dictionary[instrument]['GPVector'][0] = np.log((priors['sigma_w_'+instrument]['cvalue']*1e-6)**2.)
                # Save pooled log(variance) of the GP process:
                lc_dictionary[instrument]['GPVector'][1] = np.log((priors['GP_sigma_'+lc_dictionary[instrument]['GP_sigma']]['cvalue']*1e-6)**2.)
                # Save log-alpha:
                lc_dictionary[instrument]['GPVector'][2] = np.log(1./priors['GP_alpha_'+lc_dictionary[instrument]['GP_alpha']]['cvalue'])
                # Save the Gamma:
                lc_dictionary[instrument]['GPVector'][3] = priors['GP_Gamma_'+lc_dictionary[instrument]['GP_Gamma']]['cvalue']
                # And save log(Prot):
                lc_dictionary[instrument]['GPVector'][4] = np.log(priors['GP_Prot_'+lc_dictionary[instrument]['GP_Prot']]['cvalue'])
            if lc_dictionary[instrument]['GPType'] == 'CeleriteQPKernel':
                # Note order of GP Vector: logB, logL, logProt, logC, logJitter                  
                # Save the log(B) term of the current GP vector:
                lc_dictionary[instrument]['GPVector'][0] = np.log(priors['GP_B_'+lc_dictionary[instrument]['GP_B']]['cvalue'])
                # Save the log(L) term of the current GP vector:
                lc_dictionary[instrument]['GPVector'][1] = np.log(priors['GP_L_'+lc_dictionary[instrument]['GP_L']]['cvalue'])
                # Save the log(Prot) term of the current GP vector:
                lc_dictionary[instrument]['GPVector'][2] = np.log(priors['GP_Prot_'+lc_dictionary[instrument]['GP_Prot']]['cvalue'])
                # Save the log(C) term of the current GP vector:
                lc_dictionary[instrument]['GPVector'][3] = np.log(priors['GP_C_'+lc_dictionary[instrument]['GP_C']]['cvalue'])
                # Save the log(jitter) term of the current GP vector:
                lc_dictionary[instrument]['GPVector'][4] = np.log(priors['sigma_w_'+instrument]['cvalue']*1e-6)
            lc_dictionary[instrument]['GPObject'].set_parameter_vector(lc_dictionary[instrument]['GPVector'])
            log_likelihood += lc_dictionary[instrument]['GPObject'].log_likelihood(residuals)

    # Add the likelihood term for the stellar density:
    if (stellar_density) and (not fitrho):
        model = ((3.*np.pi)/(G*(P*(24.*3600.0))**2))*(a)**3
        log_likelihood += - 0.5*(np.log(2*np.pi) + 2.*np.log(sd_sigma) + ((model-sd_mean)/sd_sigma)**2)

    # Now RVs:
    if rvfilename is not None:
        # Before everything continues, make sure periods are chronologically ordered (this is to avoid multiple modes due to 
        # periods "jumping" between planet numbering):
        for n in range(n_rv):
            i = numbering_rv[n]
            if n == 0:
                cP = priors['P_p'+str(i)]['cvalue']
            else:
                if cP < priors['P_p'+str(i)]['cvalue']:
                    cP = priors['P_p'+str(i)]['cvalue']
                else:
                    return -1e101
            if cP < 0.:
                return -1e101

        # Now prepare RV model. Note here it is extremely important to see the distinction between n and numbering_rv[n]. n is 
        # the numbering of the planet among all the RV planets, and is what is used to tell radvel which planet is which. However, 
        # numbering_rv[n] is the planet number within the juliet framework, which extracts the parameters from the prior file.
        for n in range(n_rv):
            i = numbering_rv[n]
            K,t0,P = priors['K_p'+str(i)]['cvalue'],\
                     priors['t0_p'+str(i)]['cvalue'],\
                     priors['P_p'+str(i)]['cvalue']

            if ecc_parametrization['rv'][i] == 0:
                ecc,omega = priors['ecc_p'+str(i)]['cvalue'],priors['omega_p'+str(i)]['cvalue']*np.pi/180.
            elif ecc_parametrization['rv'][i] == 1:
                ecc = np.sqrt(priors['ecosomega_p'+str(i)]['cvalue']**2+priors['esinomega_p'+str(i)]['cvalue']**2)
                omega = np.arctan2(priors['esinomega_p'+str(i)]['cvalue'],priors['ecosomega_p'+str(i)]['cvalue'])
            else:
                ecc = priors['secosomega_p'+str(i)]['cvalue']**2+priors['sesinomega_p'+str(i)]['cvalue']**2
                omega = np.arctan2(priors['sesinomega_p'+str(i)]['cvalue'],priors['secosomega_p'+str(i)]['cvalue'])

            # Reject samples giving unphysical eccentricities:
            if ecc > 1.:
                return -1e101
            radvel_params['per'+str(n+1)] = radvel.Parameter(value=P)
            radvel_params['tc'+str(n+1)] = radvel.Parameter(value=t0)
            radvel_params['w'+str(n+1)] = radvel.Parameter(value=omega)
            radvel_params['e'+str(n+1)] = radvel.Parameter(value=ecc)
            radvel_params['k'+str(n+1)] = radvel.Parameter(value=K)
        # Compute RV model:
        rvmodel = radvel.model.RVModel(radvel_params).__call__(t_rv)

    # If added linear trend, compute its term:
    if fitrvline:
        # Line is of the form RV = aline*t + bline:
        aline = priors['rv_slope']['cvalue']
        bline = -priors['rv_tzero']['cvalue']*aline
        # Add line to the RV model:
        rvmodel += aline*t_rv + bline
    if fitrvquad:
        # Quad is of the form RV = q*t**2 + aline*t + bline:
        qquad = priors['rv_quad']['cvalue']
        aline = priors['rv_slope']['cvalue']
        bline = -priors['rv_tzero']['cvalue']*aline - (priors['rv_tzero']['cvalue']**2)*qquad
        # Add line to the RV model:
        rvmodel += qquad*(t_rv**2) + aline*t_rv + bline

    # Compute log-likelihood for each RV instrument separately in case of white-noise:
    for instrument in inames_rv:
        # If no external parameters, return added gaussian log-likelihood:
        if rveparamfile is None:
            residuals = rv_rv[instrument_indexes_rv[instrument]] - \
                        (rvmodel[instrument_indexes_rv[instrument]] + priors['mu_'+instrument]['cvalue'])
            log_likelihood += gaussian_log_likelihood(residuals,\
                              rverr_rv[instrument_indexes_rv[instrument]],\
                              priors['sigma_w_rv_'+instrument]['cvalue'])
        else:
           rvresiduals[instrument_indexes_rv[instrument]] = rv_rv[instrument_indexes_rv[instrument]] - \
                                                            (rvmodel[instrument_indexes_rv[instrument]] + priors['mu_'+instrument]['cvalue'])
           rvresiduals_err[instrument_indexes_rv[instrument]] = np.sqrt(rverr_rv[instrument_indexes_rv[instrument]]**2 + \
                                                                        priors['sigma_w_rv_'+instrument]['cvalue']**2)
    if rveparamfile is not None:
        if rv_dictionary['GPType'] == 'ExpSineSquaredSEKernel':
            # Save pooled log(variance) of the GP process:
            rv_dictionary['GPVector'][0] = np.log((priors['GP_sigma_'+rv_dictionary['GP_sigma']]['cvalue'])**2.)
            # Save log-alpha:
            rv_dictionary['GPVector'][1] = np.log(1./priors['GP_alpha_'+rv_dictionary['GP_alpha']]['cvalue'])
            # Save the Gamma:
            rv_dictionary['GPVector'][2] = priors['GP_Gamma_'+rv_dictionary['GP_Gamma']]['cvalue']
            # And save log(Prot):
            rv_dictionary['GPVector'][3] = np.log(priors['GP_Prot_'+rv_dictionary['GP_Prot']]['cvalue'])
        if rv_dictionary['GPType'] == 'CeleriteQPKernel':
            # Note order of GP Vector: logB, logL, logProt, logC, logJitter                  
            # Save the log(B) term of the current GP vector:
            rv_dictionary['GPVector'][0] = np.log(priors['GP_B_'+rv_dictionary['GP_B']]['cvalue'])
            # Save the log(L) term of the current GP vector:
            rv_dictionary['GPVector'][1] = np.log(priors['GP_L_'+rv_dictionary['GP_L']]['cvalue'])
            # Save the log(L) term of the current GP vector:
            rv_dictionary['GPVector'][2] = np.log(priors['GP_Prot_'+rv_dictionary['GP_Prot']]['cvalue'])
            # Save the log(L) term of the current GP vector:
            rv_dictionary['GPVector'][3] = np.log(priors['GP_C_'+rv_dictionary['GP_C']]['cvalue'])

        rv_dictionary['GPObject'].set_parameter_vector(rv_dictionary['GPVector'])
        rv_dictionary['GPObject'].compute(rv_dictionary['X'],yerr=rvresiduals_err)
        log_likelihood += rv_dictionary['GPObject'].log_likelihood(rvresiduals)

    # And now return joint log-likelihood:
    return log_likelihood

    

out_file = out_folder+'out_multinest_'

import pickle
# If not ran already, run dynesty or MultiNest, and save posterior samples and evidences to pickle file:
if not os.path.exists(out_folder+'posteriors.pkl'):
    out = {}
    # Run dynesty or MultiNest:
    if not use_dynesty:
        pymultinest.run(loglike, prior, n_params, \
                        n_live_points = n_live_points,\
                        max_modes=100,\
                        outputfiles_basename=out_file, resume = False, verbose = True)
        # Run and get output:
        output = pymultinest.Analyzer(outputfiles_basename=out_file, n_params = n_params)
        # Get out parameters: this matrix has (samples,n_params+1):
        posterior_samples = output.get_equal_weighted_posterior()[:,:-1]
        # Get INS lnZ:
        out['lnZ'] = output.get_stats()['global evidence']
        out['lnZerr'] = output.get_stats()['global evidence error']
    else:
        if dynamic:
            if dynesty_nthreads == 'none':
                sampler = dynesty.DynamicNestedSampler(loglike, prior, n_params, nlive=n_live_points, bound = dynesty_bound, sample = dynesty_sample)
                # Run and get output:
                sampler.run_nested()
                results = sampler.results
            else:
                from multiprocessing import Pool
                import contextlib
                nthreads = int(dynesty_nthreads)
                with contextlib.closing(Pool(processes=nthreads-1)) as executor:
                    sampler = dynesty.DynamicNestedSampler(loglike, prior, n_params, nlive=n_live_points, \
                                                           bound = dynesty_bound, sample = dynesty_sample, \
                                                           pool=executor, queue_size=nthreads)
                    sampler.run_nested()
                    results = sampler.results
                    
        else:
            if dynesty_nthreads == 'none':
                sampler = dynesty.NestedSampler(loglike, prior, n_params, nlive=n_live_points, bound = dynesty_bound, sample = dynesty_sample)
                # Run and get output:
                sampler.run_nested()
                results = sampler.results
            else:
                from multiprocessing import Pool
                nthreads = int(dynesty_nthreads)
                with contextlib.closing(Pool(processes=nthreads-1)) as executor:
                    sampler = dynesty.NestedSampler(loglike, prior, n_params, nlive=n_live_points,\
                                                    bound = dynesty_bound, sample = dynesty_sample,\
                                                    pool=executor, queue_size=nthreads)
                    sampler.run_nested()
                    results = sampler.results
        out['dynesty_output'] = results
        # Get weighted posterior:
        weights = np.exp(results['logwt'] - results['logz'][-1])
        posterior_samples = resample_equal(results.samples, weights)
        # Get lnZ:
        out['lnZ'] = results.logz[-1]
        out['lnZerr'] = results.logzerr[-1]
    # Prepare output file:
    out['posterior_samples'] = {}
    out['posterior_samples']['unnamed'] = posterior_samples
    # Extract parameters:
    pcounter = 0 
    for pname in priors.keys():
        if priors[pname]['type'] != 'fixed':
            priors[pname]['cvalue'] = np.median(posterior_samples[:,pcounter])
            out['posterior_samples'][pname] = posterior_samples[:,pcounter]
            pcounter += 1
    if efficient_bp:
        out['pu'] = pu
        out['pl'] = pl
    if use_dynesty:
        if dynamic:
            pickle.dump(out,open(out_folder+'_dynesty_DNS_posteriors.pkl','wb'))
        else:
            pickle.dump(out,open(out_folder+'_dynesty_NS_posteriors.pkl','wb'))
    else:
        pickle.dump(out,open(out_folder+'posteriors.pkl','wb'))
else:
    print('Detected output files --- extracting...')
    priors,n_transit,n_rv,numbering_transit,numbering_rv,n_params = utils.readpriors(out_folder+'priors.dat')
    if use_dynesty:
        if dynamic:
            out = pickle.load(open(out_folder+'_dynesty_DNS_posteriors.pkl','rb'))
        else:
            out = pickle.load(open(out_folder+'_dynesty_NS_posteriors.pkl','rb'))
    else:
        out = pickle.load(open(out_folder+'posteriors.pkl','rb'))
    # Extract parameters:
    for pname in priors.keys():
        if priors[pname]['type'] != 'fixed':
            priors[pname]['cvalue'] = np.median(out['posterior_samples'][pname])
    posterior_samples = out['posterior_samples']['unnamed'] 
    if 'pu' in out.keys():
        pu = out['pu']
        pl = out['pl']
        Ar = (pu - pl)/(2. + pl + pu)

print('Done!')
# Define number of samples we'll get to plot the models + uncertainties (default is all):
nsims = out['posterior_samples']['unnamed'].shape[0]

# Ok, here comes the plotting functions (*takes deep breath*).
# Colors for RV instruments:780116
rv_colors = ['#ca0020','#0571b0','#E28413','#090446','#780116','#574AE2']

# First, RV plots:
if rvfilename is not None:
    ###############################################################
    ###############################################################
    ######## FIRST PLOT (IF GP): RV V/S TIME PER INSTRUMENT #######
    ###############################################################
    ###############################################################
    # Here, we plot the RV V/S TIME per instrument; while doing 
    # this, we substract the GP component from each instrument to the 
    # RVs.

    # First, define zero-point for RV plotting time:
    zero_t_rv = int(np.min(t_rv))
    #for instrument in inames_rv:
    #    # First, we define some preambles for this plot:
    #    fig, axs = plt.subplots(2, 1, gridspec_kw = {'height_ratios':[3,1]}, figsize=(10,4))
    #    sns.set_context("talk")
    #    sns.set_style("ticks")
    #    matplotlib.rcParams['mathtext.fontset'] = 'stix'
    #    matplotlib.rcParams['font.family'] = 'STIXGeneral'
    #    matplotlib.rcParams['font.size'] = '5'
    #    matplotlib.rcParams['axes.linewidth'] = 1.2
    #    matplotlib.rcParams['xtick.direction'] = 'out'
    #    matplotlib.rcParams['ytick.direction'] = 'out'
    #    matplotlib.rcParams['lines.markeredgewidth'] = 1
    ###############################################################
    ###############################################################
    ################## FIRST PLOT: RV V/S TIME ####################
    ###############################################################
    ###############################################################
    # The first plot is RV v/s time. First, we define some preambles for this plot:
    fig, axs = plt.subplots(2, 1, gridspec_kw = {'height_ratios':[3,1]}, figsize=(10,4))
    sns.set_context("talk")
    sns.set_style("ticks")
    matplotlib.rcParams['mathtext.fontset'] = 'stix'
    matplotlib.rcParams['font.family'] = 'STIXGeneral'
    matplotlib.rcParams['font.size'] = '5'
    matplotlib.rcParams['axes.linewidth'] = 1.2
    matplotlib.rcParams['xtick.direction'] = 'out'
    matplotlib.rcParams['ytick.direction'] = 'out'
    matplotlib.rcParams['lines.markeredgewidth'] = 1
    # Substract the best-fit line, if any, to the RV data:
    if fitrvline:
        # Line is of the form RV = aline*t + bline:
        aline = priors['rv_slope']['cvalue']
        bline = -priors['rv_tzero']['cvalue']*aline
        # Add line to the RV model:
        rvline = aline*t_rv + bline
        rv_rv = rv_rv - rvline
    if fitrvquad:
        # Quad is of the form RV = q*t**2 + aline*t + bline:
        qquad = priors['rv_quad']['cvalue']
        aline = priors['rv_slope']['cvalue']
        bline = -priors['rv_tzero']['cvalue']*aline - (priors['rv_tzero']['cvalue']**2)*qquad
        # Add line to the RV model:
        rvquad = qquad*(t_rv**2) + aline*t_rv + bline
        rv_rv = rv_rv - rvquad
    # Now, iterate between the two plots; one for the RV v/s time, and one for the residuals v/s time:
    print('Plotting RV vs time...')
    for i in range(2):
        ax = axs[i]
        if i == 0:
          # Create dictionary that will save the systemic-corrected RVs (useful for the other plots!):
          sys_corrected = {}
          # First row is plot of time v/s rv plot. To plot this, first plot the data:
          color_counter =  0
          all_rv = np.zeros(len(t_rv))
          all_rv_err = np.zeros(len(t_rv))
          for instrument in inames_rv: 
              all_rv[[instrument_indexes_rv[instrument]]] = rv_rv[instrument_indexes_rv[instrument]] - priors['mu_'+instrument]['cvalue']
              all_rv_err[[instrument_indexes_rv[instrument]]] = np.sqrt(rverr_rv[instrument_indexes_rv[instrument]]**2 + priors['sigma_w_rv_'+instrument]['cvalue']**2)
              corrected_rv = rv_rv[instrument_indexes_rv[instrument]] - priors['mu_'+instrument]['cvalue']
              corrected_rv_err = np.sqrt(rverr_rv[instrument_indexes_rv[instrument]]**2 + priors['sigma_w_rv_'+instrument]['cvalue']**2)
              ax.errorbar(t_rv[instrument_indexes_rv[instrument]] - zero_t_rv,corrected_rv,\
                          yerr=corrected_rv_err,fmt='.',label=instrument.upper(),elinewidth=1,color=rv_colors[color_counter],alpha=0.5)
              color_counter += 1
              # Save systemic corrected RVs:
              sys_corrected[instrument] = {}
              sys_corrected[instrument]['values'] = corrected_rv
              sys_corrected[instrument]['errors'] = corrected_rv_err
          # Now RV model on top. For this, oversample the times:
          t_rv_model = np.linspace(np.min(t_rv)-10,np.max(t_rv)+10,5000)

          # Now compute many models, for which we'll get the quantiles later for the 
          # joint RV model. Do the same for the samples according to the times in t_rv_model 
          # (oversampled model) and for the times in t_rv_model (model with same samples as data 
          # --- useful to compute residuals):
          all_rv_models = np.zeros([nsims,len(t_rv_model)])
          all_rv_models_real = np.zeros([nsims,len(t_rv)])
          
          # If GP is on, generate a vector that will save the GP component, to be substracted later for the 
          # phased RVs:
          if rveparamfile is not None:
              all_gp_models_real = np.zeros([nsims,len(t_rv)])
          #    if rv_dictionary['GPType'] == 'ExpSineSquaredSEKernel':
          #        K1 = 1.*george.kernels.ExpSquaredKernel(metric = 1.0)
          #        K2 = george.kernels.ExpSine2Kernel(gamma=1.0,log_period=1.0)
          #        GPKernelBase = K1*K2
          #        gp = george.GP(GPKernelBase, mean=0.0,fit_mean=False,\
          #                       fit_white_noise=False,solver=george.HODLRSolver, seed=42)
          #        GPVector = np.zeros(4)
          #        gp.compute(rv_dictionary['X'],yerr=all_rv_err)

          # Here comes one of the slow parts: for the first nsims posterior samples, compute a model:
          print('Sampling models...')
          for j in range(nsims):
              # Sample the jth sample of parameter values:
              for pname in priors.keys():
                  if priors[pname]['type'] != 'fixed':
                      priors[pname]['cvalue'] = out['posterior_samples'][pname][j]

              # With those samples, compute full RV model and the planet-by-planet model:
              for n in range(n_rv):
                  iplanet = numbering_rv[n]
                  K,t0,P = priors['K_p'+str(iplanet)]['cvalue'],\
                           priors['t0_p'+str(iplanet)]['cvalue'],\
                           priors['P_p'+str(iplanet)]['cvalue']

                  if ecc_parametrization['rv'][iplanet] == 0:
                      ecc,omega = priors['ecc_p'+str(iplanet)]['cvalue'],priors['omega_p'+str(iplanet)]['cvalue']*np.pi/180.
                  elif ecc_parametrization['rv'][iplanet] == 1:
                      ecc = np.sqrt(priors['ecosomega_p'+str(iplanet)]['cvalue']**2+priors['esinomega_p'+str(iplanet)]['cvalue']**2)
                      omega = np.arctan2(priors['esinomega_p'+str(iplanet)]['cvalue'],priors['ecosomega_p'+str(iplanet)]['cvalue'])
                  else:
                      ecc = priors['secosomega_p'+str(iplanet)]['cvalue']**2+priors['sesinomega_p'+str(iplanet)]['cvalue']**2
                      omega = np.arctan2(priors['sesinomega_p'+str(iplanet)]['cvalue'],priors['secosomega_p'+str(iplanet)]['cvalue'])

                  radvel_params['per'+str(n+1)] = radvel.Parameter(value=P)
                  radvel_params['tc'+str(n+1)] = radvel.Parameter(value=t0)
                  radvel_params['w'+str(n+1)] = radvel.Parameter(value=omega)
                  radvel_params['e'+str(n+1)] = radvel.Parameter(value=ecc)
                  radvel_params['k'+str(n+1)] = radvel.Parameter(value=K)

              # Compute full RV model:
              if rveparamfile is None:
                  all_rv_models[j,:] = radvel.model.RVModel(radvel_params).__call__(t_rv_model)
                  all_rv_models_real[j,:] = radvel.model.RVModel(radvel_params).__call__(t_rv)
              else:
                  #if rv_dictionary['GPType'] == 'ExpSineSquaredSEKernel':
                  #    # Save the log(variance) of the jitter term on the current GP vector:
                  #    #rv_dictionary[instrument]['GPVector'][0] = np.log((priors['sigma_w_rv_'+instrument]['cvalue'])**2.)
                  #    # Save pooled log(variance) of the GP process:
                  #    GPVector[0] = np.log((priors['GP_sigma_'+rv_dictionary['GP_sigma']]['cvalue'])**2.)
                  #    # Save log-alpha:
                  #    GPVector[1] = np.log(1./priors['GP_alpha_'+rv_dictionary['GP_alpha']]['cvalue'])
                  #    # Save the Gamma:
                  #    GPVector[2] = priors['GP_Gamma_'+rv_dictionary['GP_Gamma']]['cvalue']
                  #    # And save log(Prot):
                  #    GPVector[3] = np.log(priors['GP_Prot_'+rv_dictionary['GP_Prot']]['cvalue'])
                  #if rv_dictionary['GPType'] == 'CeleriteQPKernel':
                  #    # Note order of GP Vector: logB, logL, logProt, logC, logJitter                  
                  #    # Save the log(B) term of the current GP vector:
                  #    GPVector[0] = np.log(priors['GP_B_'+rv_dictionary['GP_B']]['cvalue'])
                  #    # Save the log(L) term of the current GP vector:
                  #    GPVector[1] = np.log(priors['GP_L_'+rv_dictionary['GP_L']]['cvalue'])
                  #    # Save the log(L) term of the current GP vector:
                  #    GPVector[2] = np.log(priors['GP_Prot_'+rv_dictionary['GP_Prot']]['cvalue'])
                  #    # Save the log(L) term of the current GP vector:
                  #    GPVector[3] = np.log(priors['GP_C_'+rv_dictionary['GP_C']]['cvalue'])
                  rvmodel = radvel.model.RVModel(radvel_params).__call__(t_rv)
                  for instrument in inames_rv:
                      rvresiduals[instrument_indexes_rv[instrument]] = rv_rv[instrument_indexes_rv[instrument]] - \
                                                                       (rvmodel[instrument_indexes_rv[instrument]] + priors['mu_'+instrument]['cvalue'])
                      rvresiduals_err[instrument_indexes_rv[instrument]] = np.sqrt(rverr_rv[instrument_indexes_rv[instrument]]**2 + \
                                                                           priors['sigma_w_rv_'+instrument]['cvalue']**2)

                  if rv_dictionary['GPType'] == 'ExpSineSquaredSEKernel':
                      # Save pooled log(variance) of the GP process:
                      rv_dictionary['GPVector'][0] = np.log((priors['GP_sigma_'+rv_dictionary['GP_sigma']]['cvalue'])**2.)
                      # Save log-alpha:
                      rv_dictionary['GPVector'][1] = np.log(1./priors['GP_alpha_'+rv_dictionary['GP_alpha']]['cvalue'])
                      # Save the Gamma:
                      rv_dictionary['GPVector'][2] = priors['GP_Gamma_'+rv_dictionary['GP_Gamma']]['cvalue']
                      # And save log(Prot):
                      rv_dictionary['GPVector'][3] = np.log(priors['GP_Prot_'+rv_dictionary['GP_Prot']]['cvalue'])
                  if rv_dictionary['GPType'] == 'CeleriteQPKernel':
                      # Note order of GP Vector: logB, logL, logProt, logC, logJitter                  
                      # Save the log(B) term of the current GP vector:
                      rv_dictionary['GPVector'][0] = np.log(priors['GP_B_'+rv_dictionary['GP_B']]['cvalue'])
                      # Save the log(L) term of the current GP vector:
                      rv_dictionary['GPVector'][1] = np.log(priors['GP_L_'+rv_dictionary['GP_L']]['cvalue'])
                      # Save the log(L) term of the current GP vector:
                      rv_dictionary['GPVector'][2] = np.log(priors['GP_Prot_'+rv_dictionary['GP_Prot']]['cvalue'])
                      # Save the log(L) term of the current GP vector:
                      rv_dictionary['GPVector'][3] = np.log(priors['GP_C_'+rv_dictionary['GP_C']]['cvalue'])

                  rv_dictionary['GPObject'].set_parameter_vector(rv_dictionary['GPVector'])
                  rv_dictionary['GPObject'].compute(rv_dictionary['X'],yerr=rvresiduals_err)

                  # Generate Keplerians:
                  model = radvel.model.RVModel(radvel_params).__call__(t_rv_model)
                  model_real = radvel.model.RVModel(radvel_params).__call__(t_rv)
                  # Predict real values: 
                  gpmodel_real,gpvar = rv_dictionary['GPObject'].predict(rvresiduals, rv_dictionary['X'], return_var=True)
                  all_gp_models_real[j,:] = gpmodel_real
                  all_rv_models_real[j,:] = model_real + gpmodel_real
                  gpmodel,gpvar = rv_dictionary['GPObject'].predict(rvresiduals, t_rv_model, return_var=True)
                  all_rv_models[j,:] = gpmodel + model
          # Now, finally, compute the median and the quantiles (1,2 and 3-sigma) for each time sample of the 
          # oversampled model (this is the "o" before the names). 
          omedian_model = np.zeros(len(t_rv_model))
          omodel_up1, omodel_down1 = np.zeros(len(t_rv_model)),np.zeros(len(t_rv_model))
          omodel_up2, omodel_down2 = np.zeros(len(t_rv_model)),np.zeros(len(t_rv_model))
          omodel_up3, omodel_down3 = np.zeros(len(t_rv_model)),np.zeros(len(t_rv_model))

          for i_tsample in range(len(t_rv_model)):
              # Compute quantiles for the full model:
              val,valup1,valdown1 = utils.get_quantiles(all_rv_models[:,i_tsample])
              val,valup2,valdown2 = utils.get_quantiles(all_rv_models[:,i_tsample],alpha=0.95)
              val,valup3,valdown3 = utils.get_quantiles(all_rv_models[:,i_tsample],alpha=0.99)
              omedian_model[i_tsample] = val
              omodel_up1[i_tsample],omodel_down1[i_tsample] = valup1,valdown1
              omodel_up2[i_tsample],omodel_down2[i_tsample] = valup2,valdown2
              omodel_up3[i_tsample],omodel_down3[i_tsample] = valup3,valdown3
          
          # Plot model and uncertainty in model given by posterior sampling:
          ax.fill_between(t_rv_model - zero_t_rv,omodel_down1,omodel_up1,color='cornflowerblue',alpha=0.25)
          ax.fill_between(t_rv_model - zero_t_rv,omodel_down2,omodel_up2,color='cornflowerblue',alpha=0.25)
          ax.fill_between(t_rv_model - zero_t_rv,omodel_down3,omodel_up3,color='cornflowerblue',alpha=0.25)
          ax.plot(t_rv_model - zero_t_rv,omedian_model,'-',linewidth=2,color='black') 
          ax.set_xlim([np.min(t_rv)-1-zero_t_rv,np.max(t_rv)+1-zero_t_rv])
          if rvunits == 'ms':
              ax.set_ylabel('Radial velocity (m/s)')
          else:
              ax.set_ylabel('Radial velocity (km/s)')
          ax.get_xaxis().set_major_formatter(plt.NullFormatter()) 
          ax.legend(ncol=3)
        if i == 1:
          # Second row is residuals. First, compute the median real model to get the residuals:
          all_rv_models_real = np.median(all_rv_models_real,axis=0)
          # Plot a zero line to guide the eye:
          ax.plot([-1e10,1e10],[0.,0.],'--',linewidth=2,color='black')
          # Compute, plot and save the residuals:
          color_counter = 0
          fout = open(out_folder+'rv_residuals.dat','w')
          fout.write('# Time Residual Error Instrument \n')
          for instrument in inames_rv:
              ax.errorbar(t_rv[instrument_indexes_rv[instrument]] - zero_t_rv, \
                           sys_corrected[instrument]['values'] - all_rv_models_real[instrument_indexes_rv[instrument]], \
                           sys_corrected[instrument]['errors'],fmt='.',label=instrument,elinewidth=1,color=rv_colors[color_counter],alpha=0.5)
              for ii in range(len(t_rv[instrument_indexes_rv[instrument]])):
                  vals = '{0:.10f} {1:.10f} {2:.10f}'.format(t_rv[instrument_indexes_rv[instrument]][ii],sys_corrected[instrument]['values'][ii] - all_rv_models_real[instrument_indexes_rv[instrument]][ii],\
                                                             sys_corrected[instrument]['errors'][ii])
                  fout.write(vals+' '+instrument+' \n')
              color_counter += 1
          fout.close()
          ax.set_ylabel('Residuals')
          ax.set_xlabel('Time (BJD - '+str(zero_t_rv)+')')
          ax.set_xlim([np.min(t_rv)-1-zero_t_rv,np.max(t_rv)+1-zero_t_rv])
    # Plot RV vs time:
    plt.tight_layout()
    plt.savefig(out_folder+'rv_vs_time.pdf')


    ###############################################################
    ###############################################################
    ################## SECOND PLOT: PHASED RVs ####################
    ###############################################################
    ###############################################################

    # Now, plot RV for each planet; each column is a different planet. As before, preambles:
    if n_rv == 1:
        fig, axs = plt.subplots(1, n_rv, figsize=(5,4))
    else:
        fig, axs = plt.subplots(1, n_rv, figsize=(15,4))
    sns.set_context("talk")
    sns.set_style("ticks")
    matplotlib.rcParams['mathtext.fontset'] = 'stix'
    matplotlib.rcParams['font.family'] = 'STIXGeneral'
    matplotlib.rcParams['font.size'] = '5'
    matplotlib.rcParams['axes.linewidth'] = 1.2
    matplotlib.rcParams['xtick.direction'] = 'out'
    matplotlib.rcParams['ytick.direction'] = 'out'
    matplotlib.rcParams['lines.markeredgewidth'] = 1
 
    print('RV per planet...')
    # Now iterate through the planets:
    for n in range(n_rv):
        iplanet = numbering_rv[n]
        if n_rv == 1:
            ax = axs
        else:
            ax = axs[n]
        # First, generate a model that contans the components of all the other planets. For computing this, simply use the medians 
        # of the samples (i.e., the uncertainties we'll plot for each planet are the --- marginalized --- uncertainties on those planets 
        # parameters only). If GP is on, also substract best-fit GP model:
        rvmodel_minus_iplanet = np.zeros(len(t_rv))
        for nn in range(n_rv):
            i = numbering_rv[nn]
            if i != iplanet:
                K,t0,P = np.median(out['posterior_samples']['K_p'+str(i)]),\
                         np.median(out['posterior_samples']['t0_p'+str(i)]),\
                         np.median(out['posterior_samples']['P_p'+str(i)])

                if ecc_parametrization['rv'][i] == 0:
                    ecc,omega = priors['ecc_p'+str(i)]['cvalue'],priors['omega_p'+str(i)]['cvalue']*np.pi/180.
                elif ecc_parametrization['rv'][i] == 1:
                    ecc = np.sqrt(priors['ecosomega_p'+str(i)]['cvalue']**2+priors['esinomega_p'+str(i)]['cvalue']**2)
                    omega = np.arctan2(priors['esinomega_p'+str(i)]['cvalue'],priors['ecosomega_p'+str(i)]['cvalue'])
                else:
                    ecc = priors['secosomega_p'+str(i)]['cvalue']**2+priors['sesinomega_p'+str(i)]['cvalue']**2
                    omega = np.arctan2(priors['sesinomega_p'+str(i)]['cvalue'],priors['secosomega_p'+str(i)]['cvalue'])

                ecc = np.median(ecc)
                omega = np.median(omega)

                radvel_params['per'+str(nn+1)] = radvel.Parameter(value=P)
                radvel_params['tc'+str(nn+1)] = radvel.Parameter(value=t0)
                radvel_params['w'+str(nn+1)] = radvel.Parameter(value=omega)
                radvel_params['e'+str(nn+1)] = radvel.Parameter(value=ecc)
                radvel_params['k'+str(nn+1)] = radvel.Parameter(value=K)
                rvmodel_minus_iplanet += radvel.model.RVModel(radvel_params).__call__(t_rv,planet_num=nn+1) 

        # Get phases for the current planetary model. For this get median period and t0:
        P,t0 = np.median(out['posterior_samples']['P_p'+str(iplanet)]),np.median(out['posterior_samples']['t0_p'+str(iplanet)])

        # Get the actual phases:
        phases = utils.get_phases(t_rv,P,t0)

        # Now plot phased RVs minus the component model without the current planet:
        planet_rvs = np.array([])
        color_counter = 0
        if rveparamfile is not None:
            rvmodel_minus_iplanet += np.median(all_gp_models_real,axis=0)
        #all_rv_data_phases = np.array([])
        #all_rv_data_data = np.array([])
        for instrument in inames_rv:
            #all_rv_data_phases = np.append(all_rv_data_phases,phases[instrument_indexes_rv[instrument]])
            #all_rv_data_data = np.append(all_rv_data_data,\
            #                   sys_corrected[instrument]['values']-rvmodel_minus_iplanet[instrument_indexes_rv[instrument]])
            ax.errorbar(phases[instrument_indexes_rv[instrument]],\
                        sys_corrected[instrument]['values']-rvmodel_minus_iplanet[instrument_indexes_rv[instrument]],\
                        yerr=sys_corrected[instrument]['errors'],fmt='o',ms=4,elinewidth=1,color=rv_colors[color_counter],alpha=0.5)  
            # This following array is useful for computing limits of the plot:
            planet_rvs = np.append(planet_rvs,sys_corrected[instrument]['values']-rvmodel_minus_iplanet[instrument_indexes_rv[instrument]])
            color_counter += 1
       
        #idx_phases_sorted = np.argsort(all_rv_data_phases)
        #phases_bin,rv_bin,rv_bin_err = utils.bin_data(all_rv_data_phases[idx_phases_sorted],\
        #                                            all_rv_data_data[idx_phases_sorted],5)
        #ax.errorbar(phases_bin,rv_bin,yerr=rv_bin_err,fmt='o',mec='black',mfc='white',elinewidth=1,ecolor='black')
        # Now, as in the previous plot, sample models from the posterior parameters along the phases of interest. 
        # For this, first define a range of phases of interest:
        model_phases = np.linspace(-0.6,0.6,10000) 

        # With this get the respective times for the model phases:
        t_model_phases = model_phases*P + t0

        # Now generate the models:
        all_rv_models = np.zeros([nsims,len(t_model_phases)])

        for j in range(nsims):
            # Sample the jth sample of parameter values:
            for pname in priors.keys():
                if priors[pname]['type'] != 'fixed':
                    priors[pname]['cvalue'] = out['posterior_samples'][pname][j]

            # With those samples, compute full RV model and the planet-by-planet model:
            K,t0,P = priors['K_p'+str(iplanet)]['cvalue'],\
                     priors['t0_p'+str(iplanet)]['cvalue'],\
                     priors['P_p'+str(iplanet)]['cvalue']

            if ecc_parametrization['rv'][iplanet] == 0:
                ecc,omega = priors['ecc_p'+str(iplanet)]['cvalue'],priors['omega_p'+str(iplanet)]['cvalue']*np.pi/180.
            elif ecc_parametrization['rv'][iplanet] == 1:
                ecc = np.sqrt(priors['ecosomega_p'+str(iplanet)]['cvalue']**2+priors['esinomega_p'+str(iplanet)]['cvalue']**2)
                omega = np.arctan2(priors['esinomega_p'+str(iplanet)]['cvalue'],priors['ecosomega_p'+str(iplanet)]['cvalue'])
            else:
                ecc = priors['secosomega_p'+str(iplanet)]['cvalue']**2+priors['sesinomega_p'+str(iplanet)]['cvalue']**2
                omega = np.arctan2(priors['sesinomega_p'+str(iplanet)]['cvalue'],priors['secosomega_p'+str(iplanet)]['cvalue'])

            radvel_params['per'+str(n+1)] = radvel.Parameter(value=P)
            radvel_params['tc'+str(n+1)] = radvel.Parameter(value=t0)
            radvel_params['w'+str(n+1)] = radvel.Parameter(value=omega)
            radvel_params['e'+str(n+1)] = radvel.Parameter(value=ecc)
            radvel_params['k'+str(n+1)] = radvel.Parameter(value=K)

            # Compute full RV model:
            all_rv_models[j,:] = radvel.model.RVModel(radvel_params).__call__(t_model_phases,planet_num=n+1)

        # As before, once again compute median model and the respective error bands:
        omedian_model = np.zeros(len(t_model_phases))
        omodel_up1, omodel_down1 = np.zeros(len(t_model_phases)),np.zeros(len(t_model_phases))
        omodel_up2, omodel_down2 = np.zeros(len(t_model_phases)),np.zeros(len(t_model_phases))
        omodel_up3, omodel_down3 = np.zeros(len(t_model_phases)),np.zeros(len(t_model_phases))

        for i_tsample in range(len(t_model_phases)):
            # Compute quantiles for the full model:
            val,valup1,valdown1 = utils.get_quantiles(all_rv_models[:,i_tsample])
            val,valup2,valdown2 = utils.get_quantiles(all_rv_models[:,i_tsample],alpha=0.95)
            val,valup3,valdown3 = utils.get_quantiles(all_rv_models[:,i_tsample],alpha=0.99)
            omedian_model[i_tsample] = val
            omodel_up1[i_tsample],omodel_down1[i_tsample] = valup1,valdown1
            omodel_up2[i_tsample],omodel_down2[i_tsample] = valup2,valdown2
            omodel_up3[i_tsample],omodel_down3[i_tsample] = valup3,valdown3

        # Now plot the phased model. Compute sorting indexes as well and plot sorted phases:
        ax.fill_between(model_phases,omodel_down1,omodel_up1,color='cornflowerblue',alpha=0.25)
        ax.fill_between(model_phases,omodel_down2,omodel_up2,color='cornflowerblue',alpha=0.25)
        ax.fill_between(model_phases,omodel_down3,omodel_up3,color='cornflowerblue',alpha=0.25)
        ax.plot(model_phases,omedian_model,'-',linewidth=2,color='black')
        ax.set_xlim([-0.5,0.5])       
        #out['posterior_samples'][pname]
        P,t0 = np.median(out['posterior_samples']['P_p'+str(iplanet)]),\
               np.median(out['posterior_samples']['t0_p'+str(iplanet)])
        print(P,t0,iplanet)
        title_text = r'$P={0:.3f}$, $t_0 = {1:.5f}$'.format(P,t0)
        if n_rv>1:
            ax.set_title('Planet '+str(iplanet)+': '+title_text)
        if iplanet == 0:
            if rvunits == 'ms':
                ax.set_ylabel('Radial velocity (m/s)')
            else:
                ax.set_ylabel('Radial velocity (km/s)')
        yval_lim = np.max([np.abs(np.min(omodel_down3)),np.abs(np.max(omodel_up3)),3.*np.sqrt(np.var(planet_rvs))])
        ax.set_ylim([-yval_lim,yval_lim])
        ax.set_xlabel('Phase')

    # Plot RV v/s phase for each planet:
    plt.tight_layout()
    plt.savefig(out_folder+'rvs_planets.pdf')

# Finally, transit plots:
if lcfilename is not None:
    ###############################################################
    ###############################################################
    ########### THIRD PLOT: PHOTOMETRY BY INSTRUMENT  #############
    ###############################################################
    ###############################################################


    ###############################################################
    ###############################################################
    ########### FOURTH PLOT: PHOTOMETRY BY PLANET  ################
    ###############################################################
    ###############################################################

    # Phased transits of each planet for each instrument on different plots:
    for nplanet in range(n_transit):
      iplanet = numbering_transit[nplanet]
      for instrument in inames_lc:
        fig, axs = plt.subplots(2, 1,gridspec_kw = {'height_ratios':[3,1]}, figsize=(8.4,7))
        sns.set_context("talk")
        sns.set_style("ticks")
        matplotlib.rcParams['mathtext.fontset'] = 'stix'
        matplotlib.rcParams['font.family'] = 'STIXGeneral'
        matplotlib.rcParams['font.size'] = '5'
        matplotlib.rcParams['axes.linewidth'] = 1.2
        matplotlib.rcParams['xtick.direction'] = 'out'
        matplotlib.rcParams['ytick.direction'] = 'out'
        matplotlib.rcParams['lines.markeredgewidth'] = 1

        # First, get phases for the current planetary model. For this get median period and t0:
        P,t0 = np.median(out['posterior_samples']['P_p'+str(iplanet)]),np.median(out['posterior_samples']['t0_p'+str(iplanet)])

        # Get the actual phases:
        phases = utils.get_phases(t_lc[instrument_indexes_lc[instrument]],P,t0)

        # Now, as in the previous plot, sample models from the posterior parameters along the phases of interest. 
        # For this, first define a range of phases of interest:
        if not lc_dictionary[instrument]['GPDetrend']:
            model_phases = np.linspace(-0.25,0.25,10000)
            # With this get the respective times for the model phases:
            t_model_phases = model_phases*P + t0
        else:
            model_phases = phases#np.linspace(-0.12,0.12,1000)     
            t_model_phases = t_lc[instrument_indexes_lc[instrument]]

        # If GP detrend for the current instrument, interpolate the X values; zero them for values outside 
        # the interpolation area:
        if lc_dictionary[instrument]['GPDetrend']:
            X_model = lc_dictionary[instrument]['X']
            #X_model = np.zeros([len(t_model_phases),lc_dictionary[instrument]['X'].shape[1]])
            #min_t,max_t = np.min(t_lc[instrument_indexes_lc[instrument]]),np.max(t_lc[instrument_indexes_lc[instrument]])
            #for i in range(lc_dictionary[instrument]['X'].shape[1]):
            #    xi = lc_dictionary[instrument]['X'][:,i]
            #    interp_function = interp1d(t_lc[instrument_indexes_lc[instrument]], xi)
            #    idx_interp = np.where((t_model_phases>min_t)&(t_model_phases<max_t))[0]
            #    X_model[idx_interp,i] = interp_function(t_model_phases[idx_interp])
            

        # Initialize model:
        if lc_dictionary[instrument]['resampling']:
            params_model, m_model = init_batman(t_model_phases, law=lc_dictionary[instrument]['ldlaw'], n_ss=lc_dictionary[instrument]['nresampling'], exptime_ss=lc_dictionary[instrument]['exptimeresampling'])
        else:
            params_model, m_model = init_batman(t_model_phases, law=lc_dictionary[instrument]['ldlaw'])

        # Now generate the (oversampled and "real", with the real data sampling) models:
        all_lc_models = np.zeros([nsims,len(t_model_phases)])
        all_lc_real_models = np.zeros([nsims,len(phases)])
        lcmodel = np.ones(len(t_model_phases))
        lcmodel_real = np.ones(len(phases))
        for j in range(nsims):
            # Sample the jth sample of parameter values:
            for pname in priors.keys():
                if priors[pname]['type'] != 'fixed':
                    priors[pname]['cvalue'] = out['posterior_samples'][pname][j]

            if lc_dictionary[instrument]['ldlaw'] != 'linear':
                coeff1,coeff2 = reverse_ld_coeffs(lc_dictionary[instrument]['ldlaw'],priors['q1_'+instrument]['cvalue'],\
                                priors['q2_'+instrument]['cvalue'])
                params_model.u = [coeff1,coeff2]
            else:
                params_model.u = [priors['q1_'+instrument]['cvalue']]

            if efficient_bp:
                if not fitrho:
                    a,r1,r2,t0,P = priors['a_p'+str(iplanet)]['cvalue'],priors['r1_p'+str(iplanet)]['cvalue'],\
                                   priors['r2_p'+str(iplanet)]['cvalue'], priors['t0_p'+str(iplanet)]['cvalue'], \
                                   priors['P_p'+str(iplanet)]['cvalue']
                else:
                    rho,r1,r2,t0,P = priors['rho']['cvalue'],priors['r1_p'+str(iplanet)]['cvalue'],\
                                   priors['r2_p'+str(iplanet)]['cvalue'], priors['t0_p'+str(iplanet)]['cvalue'], \
                                   priors['P_p'+str(iplanet)]['cvalue']
                    a = ((rho*G*((P*24.*3600.)**2))/(3.*np.pi))**(1./3.)
                if r1 > Ar:
                    b,p = (1+pl)*(1. + (r1-1.)/(1.-Ar)),\
                          (1-r2)*pl + r2*pu
                else:
                    b,p = (1. + pl) + np.sqrt(r1/Ar)*r2*(pu-pl),\
                          pu + (pl-pu)*np.sqrt(r1/Ar)*(1.-r2)
            else:
                if not fitrho:
                    a,b,p,t0,P = priors['a_p'+str(iplanet)]['cvalue'],priors['b_p'+str(iplanet)]['cvalue'],\
                                 priors['p_p'+str(iplanet)]['cvalue'], priors['t0_p'+str(iplanet)]['cvalue'], \
                                 priors['P_p'+str(iplanet)]['cvalue']
                else:
                    rho,b,p,t0,P = priors['rho']['cvalue'],priors['b_p'+str(iplanet)]['cvalue'],\
                                   priors['p_p'+str(iplanet)]['cvalue'], priors['t0_p'+str(iplanet)]['cvalue'], \
                                   priors['P_p'+str(iplanet)]['cvalue']
                    a = ((rho*G*((P*24.*3600.)**2))/(3.*np.pi))**(1./3.)

            if ecc_parametrization['transit'][iplanet] == 0:
                ecc,omega = priors['ecc_p'+str(iplanet)]['cvalue'],priors['omega_p'+str(iplanet)]['cvalue']
            elif ecc_parametrization['transit'][iplanet] == 1:
                ecc = np.sqrt(priors['ecosomega_p'+str(iplanet)]['cvalue']**2+priors['esinomega_p'+str(iplanet)]['cvalue']**2)
                omega = np.arctan2(priors['esinomega_p'+str(iplanet)]['cvalue'],priors['ecosomega_p'+str(iplanet)]['cvalue'])*(180/np.pi)
            else:
                ecc = priors['secosomega_p'+str(iplanet)]['cvalue']**2+priors['sesinomega_p'+str(iplanet)]['cvalue']**2
                omega = np.arctan2(priors['sesinomega_p'+str(iplanet)]['cvalue'],priors['secosomega_p'+str(iplanet)]['cvalue'])*(180/np.pi)

            ecc_factor = (1. + ecc*np.sin(omega * np.pi/180.))/(1. - ecc**2)
            inc_inv_factor = (b/a)*ecc_factor
            if not (b>1.+p or inc_inv_factor >=1.):
                inc = np.arccos(inc_inv_factor)*180./np.pi
                params_model.t0 = t0
                params_model.per = P
                params_model.rp = p
                params_model.a = a
                params_model.inc = inc
                params_model.ecc = ecc
                params_model.w = omega
                #print 't0',t0,'P',P,'p',p,'a',a,'inc',inc,'ecc,omega',ecc,omega,'q1',priors['q1_p'+str(iplanet)]['cvalue'],'q2',priors['q2_p'+str(iplanet)]['cvalue'],'coeff1,coeff2',coeff1,coeff2

                if not lc_dictionary[instrument]['GPDetrend']:
                    all_lc_models[j,:] = m_model.light_curve(params_model)*priors['mdilution_'+instrument]['cvalue'] + \
                                         priors['mflux_'+instrument]['cvalue']
                    all_lc_real_models[j,:] = lc_dictionary[instrument]['m'].light_curve(params_model)*priors['mdilution_'+instrument]['cvalue'] + \
                                              priors['mflux_'+instrument]['cvalue']
                else:
                    # Set current values to GP Vector:
                    #lc_dictionary[instrument]['GPVector'][0] = np.log((priors['sigma_w_'+instrument]['cvalue']*1e-6)**2.)
                    #lc_dictionary[instrument]['GPVector'][1] = np.log((priors['GP_sigma_'+lc_dictionary[instrument]['GP_sigma']]['cvalue']*1e-6)**2.)
                    #for i in range(lc_dictionary[instrument]['nX']):
                    #    lc_dictionary[instrument]['GPVector'][2+i] = np.log(1./priors['GP_alpha'+str(i)+'_'+lc_dictionary[instrument]['GP_alpha'+str(i)]]['cvalue'])
                    #lc_dictionary[instrument]['GPObject'].set_parameter_vector(lc_dictionary[instrument]['GPVector'])
                    

                    if lc_dictionary[instrument]['GPType'] == 'SEKernel':
                        # Save the log(variance) of the jitter term on the current GP vector:
                        lc_dictionary[instrument]['GPVector'][0] = np.log((priors['sigma_w_'+instrument]['cvalue']*1e-6)**2.)
                        # Save pooled variance of the GP process:
                        lc_dictionary[instrument]['GPVector'][1] = np.log((priors['GP_sigma_'+lc_dictionary[instrument]['GP_sigma']]['cvalue']*1e-6)**2.)
                        # Now save (log of) coefficients of each GP term:
                        for i in range(lc_dictionary[instrument]['nX']):
                            lc_dictionary[instrument]['GPVector'][2+i] = np.log(1./priors['GP_alpha'+str(i)+'_'+lc_dictionary[instrument]['GP_alpha'+str(i)]]['cvalue'])
                    if lc_dictionary[instrument]['GPType'] == 'ExpSineSquaredSEKernel':
                        # Save the log(variance) of the jitter term on the current GP vector:
                        lc_dictionary[instrument]['GPVector'][0] = np.log((priors['sigma_w_'+instrument]['cvalue']*1e-6)**2.)
                        # Save pooled log(variance) of the GP process:
                        lc_dictionary[instrument]['GPVector'][1] = np.log((priors['GP_sigma_'+lc_dictionary[instrument]['GP_sigma']]['cvalue']*1e-6)**2.)
                        # Save log-alpha:
                        lc_dictionary[instrument]['GPVector'][2] = np.log(1./priors['GP_alpha_'+lc_dictionary[instrument]['GP_alpha']]['cvalue'])
                        # Save the Gamma:
                        lc_dictionary[instrument]['GPVector'][3] = priors['GP_Gamma_'+lc_dictionary[instrument]['GP_Gamma']]['cvalue']
                        # And save log(Prot):
                        lc_dictionary[instrument]['GPVector'][4] = np.log(priors['GP_Prot_'+lc_dictionary[instrument]['GP_Prot']]['cvalue'])
                    if lc_dictionary[instrument]['GPType'] == 'CeleriteQPKernel':
                        # Note order of GP Vector: logB, logL, logProt, logC, logJitter                  
                        # Save the log(B) term of the current GP vector:
                        lc_dictionary[instrument]['GPVector'][0] = np.log(priors['GP_B_'+lc_dictionary[instrument]['GP_B']]['cvalue'])
                        # Save the log(L) term of the current GP vector:
                        lc_dictionary[instrument]['GPVector'][1] = np.log(priors['GP_L_'+lc_dictionary[instrument]['GP_L']]['cvalue'])
                        # Save the log(L) term of the current GP vector:
                        lc_dictionary[instrument]['GPVector'][2] = np.log(priors['GP_Prot_'+lc_dictionary[instrument]['GP_Prot']]['cvalue'])
                        # Save the log(L) term of the current GP vector:
                        lc_dictionary[instrument]['GPVector'][3] = np.log(priors['GP_C_'+lc_dictionary[instrument]['GP_C']]['cvalue'])
                        # Save the log(L) term of the current GP vector:
                        lc_dictionary[instrument]['GPVector'][4] = np.log(priors['sigma_w_'+instrument]['cvalue']*1e-6)
                    lc_dictionary[instrument]['GPObject'].set_parameter_vector(lc_dictionary[instrument]['GPVector'])

                    # Generate deterministic model for the real data:
                    deterministic_model = lc_dictionary[instrument]['m'].light_curve(params_model)*priors['mdilution_'+instrument]['cvalue'] + \
                                              priors['mflux_'+instrument]['cvalue'] 
                    # Generate residuals with this model:
                    residuals = f_lc[instrument_indexes_lc[instrument]] - deterministic_model
                    # Predict sampled points:
                    pred_mean,pred_var = lc_dictionary[instrument]['GPObject'].predict(residuals, lc_dictionary[instrument]['X'], return_var=True)
                    all_lc_real_models[j,:] = deterministic_model + pred_mean
                    # Predict interpolated model
                    pred_mean,pred_var = lc_dictionary[instrument]['GPObject'].predict(residuals, X_model, return_var=True)
                    #pred_mean = lc_dictionary[instrument]['GPObject'].sample_conditional(residuals, X_model)#, return_var=True)
                    # Generate interpolated deterministic model plus GP 
                    all_lc_models[j,:] = pred_mean + m_model.light_curve(params_model)*priors['mdilution_'+instrument]['cvalue'] + \
                                         priors['mflux_'+instrument]['cvalue']
                    

        # As before, once again compute median model and the respective error bands:
        omedian_model = np.zeros(len(t_model_phases))
        omodel_up1, omodel_down1 = np.zeros(len(t_model_phases)),np.zeros(len(t_model_phases))
        omodel_up2, omodel_down2 = np.zeros(len(t_model_phases)),np.zeros(len(t_model_phases))
        omodel_up3, omodel_down3 = np.zeros(len(t_model_phases)),np.zeros(len(t_model_phases))

        for i_tsample in range(len(t_model_phases)):
            # Compute quantiles for the full model:
            val,valup1,valdown1 = utils.get_quantiles(all_lc_models[:,i_tsample])
            val,valup2,valdown2 = utils.get_quantiles(all_lc_models[:,i_tsample],alpha=0.95)
            val,valup3,valdown3 = utils.get_quantiles(all_lc_models[:,i_tsample],alpha=0.99)
            omedian_model[i_tsample] = val
            omodel_up1[i_tsample],omodel_down1[i_tsample] = valup1,valdown1
            omodel_up2[i_tsample],omodel_down2[i_tsample] = valup2,valdown2
            omodel_up3[i_tsample],omodel_down3[i_tsample] = valup3,valdown3

        lcmodel = np.zeros(len(phases))
        # Do the same for the "real" sampling of the data:
        for i_tsample in range(len(phases)):
            #val,valup1,valdown1 = utils.get_quantiles(all_lc_real_models[:,i_tsample])
            lcmodel[i_tsample] = np.median(all_lc_real_models[:,i_tsample])

        ax = axs[0]
        # Plot data:
        ax.plot(phases,f_lc[instrument_indexes_lc[instrument]],'.k',markersize=5,alpha=0.2)
        # Plot binned data:
        phases_bin,f_bin,f_bin_err = utils.bin_data(phases,f_lc[instrument_indexes_lc[instrument]],15)
        ax.errorbar(phases_bin,f_bin,yerr=f_bin_err,fmt='.k',markersize=5,elinewidth=1,alpha=0.4)

        fout = open(out_folder+'phased_lc_'+instrument+'.dat','w')
        fout.write('# Phases \t Time \t Phased LC \t Model\n')
        for i in range(len(phases)):
            fout.write('{0:.10f} {1:.10f} {2:.10f} {3:.10f}\n'.format(phases[i],t_lc[i],f_lc[instrument_indexes_lc[instrument]][i],lcmodel[i]))
        fout.close() 
        # Now plot the phased model. Compute sorting indexes as well and plot sorted phases:
        ax.fill_between(model_phases,omodel_down1,omodel_up1,color='cornflowerblue',alpha=0.25)
        ax.fill_between(model_phases,omodel_down2,omodel_up2,color='cornflowerblue',alpha=0.25)
        ax.fill_between(model_phases,omodel_down3,omodel_up3,color='cornflowerblue',alpha=0.25)
        ax.plot(model_phases,omedian_model,'-',linewidth=2,color='black')
        ax.set_ylabel('Relative flux')

        if not efficient_bp:
            depth = np.median(out['posterior_samples']['p_p'+str(iplanet)])**2#priors['p_p'+str(iplanet)]['cvalue']
        else:
            depth = np.array([])
            for i in range(len(out['posterior_samples']['r1_p'+str(iplanet)])):
                r1,r2 = out['posterior_samples']['r1_p'+str(iplanet)][i],out['posterior_samples']['r2_p'+str(iplanet)][i]
                if r1 > Ar:
                    b,p = (1+pl)*(1. + (r1-1.)/(1.-Ar)),\
                          (1-r2)*pl + r2*pu
                else:
                    b,p = (1. + pl) + np.sqrt(r1/Ar)*r2*(pu-pl),\
                          pu + (pl-pu)*np.sqrt(r1/Ar)*(1.-r2)       
                depth = np.append(depth,p**2)
            depth = np.median(depth)    
        if lc_dictionary[instrument]['GPDetrend']:
            ax.set_ylim([1- depth - depth*0.5,1.001 + depth*0.5+0.001])
        else:
            if depth*1e6 > 1000.:
                ax.set_ylim([1- depth - depth*0.5 -1000*1e-6,1.001 + depth*0.5])
            else:
                ax.set_ylim([1 - 1000*1e-6,1.001 + depth*0.5])

        if depth*1e6 > 1000.:
            ax.set_xlim([-0.1,0.1])
        else:
            ax.set_xlim([-0.15,0.15])

        ax.get_xaxis().set_major_formatter(plt.NullFormatter())

        # Plot residuals:
        ax = axs[1]
        # Plot zero line to guide the eye:
        ax.plot([-1e10,1e10],[0.,0.],'--',linewidth=2,color='black')
        # Plot residuals:
        ax.plot(phases,(f_lc[instrument_indexes_lc[instrument]]-lcmodel)*1e6,'.k',markersize=5,alpha=0.2)
        phases_bin,f_bin,f_bin_err = utils.bin_data(phases,(f_lc[instrument_indexes_lc[instrument]]-lcmodel)*1e6,15)
        ax.errorbar(phases_bin,f_bin,yerr=f_bin_err,fmt='.k',markersize=5,elinewidth=1,alpha=0.4)
        ax.set_ylabel('Residuals (ppm)')
        ax.set_xlabel('Phase')
        #ax.set_xlim([-0.1,0.1])
        if depth*1e6 > 1000.:
            ax.set_xlim([-0.1,0.1])
            ax.set_ylim([-5000,5000])
        else:
            ax.set_xlim([-0.15,0.15])
            ax.set_ylim([-1000,1000])
        plt.tight_layout()
        plt.savefig(out_folder+'phot_planet'+str(iplanet)+'_instrument_'+instrument+'.pdf')