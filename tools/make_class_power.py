"""This module creates a matter power spectrum using Classylss,
a python interface to the CLASS Boltzmann code.

See:
http://classylss.readthedocs.io/en/stable/
http://class-code.net/
It parses an MP-GenIC parameter file and generates a matter power spectrum
and transfer function at the initial redshift. It generates a second transfer
function at a slightly lower redshift to allow computing the growth function.
Files are saved where MP-GenIC expects to read them. Existing files will not be over-written.

Script should be compatible with python 2.7 and 3.

Cite CLASS paper:
 D. Blas, J. Lesgourgues, T. Tram, arXiv:1104.2933 [astro-ph.CO], JCAP 1107 (2011) 034
Call with:
    python make_class_power.py <MP-GenIC parameter file> <external power spectrum file>
    where the second external power spectrum file is optional and is a primordial power spectrum for CLASS."""

from __future__ import print_function
import sys
import math
import os.path
import numpy as np
import argparse
import classylss
import classylss.binding as CLASS
import configobj
import validate

GenICconfigspec = """
FileWithInputSpectrum = string(default='')
FileWithTransferFunction = string(default='')
Ngrid = integer(min=0)
BoxSize = float(min=0)
Omega0 = float(0,1)
OmegaLambda = float(0,1)
OmegaBaryon = float(0,1,default=0.0486)
HubbleParam = float(0,2)
Redshift = float(0,1100)
Sigma8 = float(default=-1)
InputPowerRedshift = float(default=-1)
DifferentTransferFunctions = integer(0,1, default=1)
UnitLength_in_cm  = float(default=3.085678e21)
Omega_fld = float(0,1,default=0)
w0_fld = float(default=-1)
wa_fld = float(default=0)
MNue = float(min=0, default=0)
MNum = float(min=0, default=0)
MNut = float(min=0, default=0)
MWDM_Therm = float(min=0, default=0)
PrimordialIndex = float(default=0.971)
PrimordialAmp = float(default=2.215e-9)
CMBTemperature = float(default=2.7255)""".split('\n')

def _check_genic_config(config):
    """Check that the MP-GenIC config file is sensible for running CLASS on."""
    vtor = validate.Validator()
    config.validate(vtor)
    filekeys = ['FileWithInputSpectrum', ]
    if config['DifferentTransferFunctions'] == 1.:
        filekeys += ['FileWithTransferFunction',]
    for ff in filekeys:
        if config[ff] == '':
            raise IOError("No savefile specified for ",ff)
        if os.path.exists(config[ff]):
            raise IOError("Refusing to write to existing file: ",config[ff])

    #Check unsupported configurations
    if config['MWDM_Therm'] > 0:
        raise ValueError("Warm dark matter power spectrum cutoff not yet supported.")
    if config['DifferentTransferFunctions'] == 1.:
        if config['InputPowerRedshift'] >= 0:
            raise ValueError("Rescaling with different transfer functions not supported.")

def _build_cosmology_params(config):
    """Build a correctly-named-for-class set of cosmology parameters from the MP-GenIC config file."""
    #Class takes omega_m h^2 as parameters
    h0 = config['HubbleParam']
    omeganu = (config['MNue'] + config['MNum'] + config['MNut'])/93.14/h0**2
    if config['OmegaBaryon'] < 0.001:
        config['OmegaBaryon'] = 0.0486
    ocdm = config['Omega0'] - config['OmegaBaryon'] - omeganu
    omegak = 1-config['OmegaLambda']-config['Omega0']
    gparams = {'h':config['HubbleParam'], 'Omega_cdm':ocdm,'Omega_b':config['OmegaBaryon'], 'Omega_k':omegak, 'n_s': config['PrimordialIndex'],'T_cmb':config["CMBTemperature"]}
    #One may specify either OmegaLambda or Omega_fld,
    #and the other is worked out by summing all matter to unity.
    #Specify Omega_fld even if we have Lambda, to avoid floating point.
    gparams['Omega_fld'] = config['Omega_fld']
    if config['Omega_fld'] > 0:
        gparams['w0_fld'] = config['w0_fld']
        gparams['wa_fld'] = config['wa_fld']
    #Set up massive neutrinos
    if omeganu > 0:
        gparams['m_ncdm'] = '%.2f,%.2f,%.2f' % (config['MNue'], config['MNum'], config['MNut'])
        gparams['N_ncdm'] = 3
        gparams['N_ur'] = 0.00641
        #Neutrino accuracy: Default pk_ref.pre has tol_ncdm_* = 1e-10,
        #which takes 45 minutes (!) on my laptop.
        #tol_ncdm_* = 1e-8 takes 20 minutes and is machine-accurate.
        #Default parameters are fast but off by 2%.
        #I chose 1e-5, which takes 6 minutes and is accurate to 1e-5
        gparams['tol_ncdm_newtonian'] = 1e-5
        gparams['tol_ncdm_synchronous'] = 1e-5
        gparams['tol_ncdm_bg'] = 1e-10
        gparams['l_max_ncdm'] = 50
        #This disables the fluid approximations, which make P_nu not match camb on small scales.
        #We need accurate P_nu to initialise our neutrino code.
        gparams['ncdm_fluid_approximation'] = 3
    else:
        gparams['N_ur'] = 3.046
    #Power spectrum amplitude
    if config['Sigma8'] > 0:
        gparams['sigma8'] = config['Sigma8']
    else:
        #Pivot scale is by default 0.05 1/Mpc! This number is NOT what is reported by Planck.
        gparams['A_s'] = config["PrimordialAmp"]
    return gparams

def make_class_power(paramfile, external_pk = None, extraz=None, verbose=False):
    """Main routine: parses a parameter file and makes a matter power spectrum.
    Will not over-write power spectra if already present.
    Options are loaded from the MP-GenIC parameter file.
    Supported:
        - Omega_fld and DE parameters.
        - Massive neutrinos.
        - Using Sigma8 to set the power spectrum scale.
        - Different transfer functions.

    We use class velocity transfer functions to have accurate initial conditions
    even on superhorizon scales, and to properly support multiple species.
    The alternative is to use rescaling.

    Not supported:
        - Warm dark matter power spectra.
        - Rescaling with different transfer functions."""
    config = configobj.ConfigObj(infile=paramfile, configspec=GenICconfigspec, file_error=True)
    #Input sanitisation
    _check_genic_config(config)

    #Precision
    pre_params = {'tol_background_integration': 1e-9, 'tol_perturb_integration' : 1.e-7, 'tol_thermo_integration':1.e-5, 'k_per_decade_for_pk': 20,'k_per_decade_for_bao':  200, 'neglect_CMB_sources_below_visibility' : 1.e-30, 'transfer_neglect_late_source': 3000., 'l_max_g' : 50, 'l_max_ur':150}

    #Important! Densities are in synchronous gauge!
    pre_params['gauge'] = 'synchronous'

    gparams = _build_cosmology_params(config)
    pre_params.update(gparams)
    redshift = config['Redshift']
    if config['InputPowerRedshift'] >= 0:
        redshift = config['InputPowerRedshift']
    outputs = np.array([redshift, ])
    if extraz is not None:
        outputs = np.concatenate([outputs, extraz])
    #Pass options for the power spectrum
    MPC_in_cm = 3.085678e24
    boxmpc = config['BoxSize'] / MPC_in_cm * config['UnitLength_in_cm']
    maxk = 2*math.pi/boxmpc*config['Ngrid']*16
    powerparams = {'output': 'dTk vTk mPk', 'P_k_max_h/Mpc' : maxk, "z_max_pk" : 1+np.max(outputs),'z_pk': outputs, 'extra metric transfer functions': 'y'}
    pre_params.update(powerparams)

    if verbose:
        verb_params = {'input_verbose': 1, 'background_verbose': 1, 'thermodynamics_verbose': 1, 'perturbations_verbose': 1, 'transfer_verbose': 1, 'primordial_verbose': 1, 'spectra_verbose': 1, 'nonlinear_verbose': 1, 'lensing_verbose': 1, 'output_verbose': 1}
        pre_params.update(verb_params)

    #Specify an external primordial power spectrum
    if external_pk is not None:
        pre_params['P_k_ini'] = "external_pk"
        pre_params["command"] = "cat ",external_pk

    #Make the power spectra module
    engine = CLASS.ClassEngine(pre_params)
    powspec = CLASS.Spectra(engine)

    #Save directory
    sdir = os.path.split(paramfile)[0]
    #Get and save the transfer functions if needed
    trans = powspec.get_transfer(z=redshift)
    if config['DifferentTransferFunctions'] == 1.:
        tfile = os.path.join(sdir, config['FileWithTransferFunction'])
        if os.path.exists(tfile):
            raise IOError("Refusing to write to existing file: ",transferfile)
        save_transfer(trans, tfile)
    #fp-roundoff
    trans['k'][-1] *= 0.9999
    #Get and save the matter power spectrum
    pk_lin = powspec.get_pklin(k=trans['k'], z=redshift)
    pkfile = os.path.join(sdir, config['FileWithInputSpectrum'])
    if os.path.exists(pkfile):
        raise IOError("Refusing to write to existing file: ",pkfile)
    np.savetxt(pkfile, np.vstack([trans['k'], pk_lin]).T)
    if extraz is not None:
        for red in extraz:
            trans = powspec.get_transfer(z=red)
            tfile = os.path.join(sdir, config['FileWithTransferFunction']+"-"+str(red))
            if os.path.exists(tfile):
                raise IOError("Refusing to write to existing file: ",transferfile)
            save_transfer(trans, tfile)
            trans['k'][-1] *= 0.9999
            #Get and save the matter power spectrum
            pk_lin = powspec.get_pklin(k=trans['k'], z=red)
            pkfile = os.path.join(sdir, config['FileWithInputSpectrum']+"-"+str(red))
            if os.path.exists(pkfile):
                raise IOError("Refusing to write to existing file: ",pkfile)
            np.savetxt(pkfile, np.vstack([trans['k'], pk_lin]).T)

def save_transfer(transfer, transferfile):
    """Save a transfer function. Note we save the CLASS FORMATTED transfer functions.
    The transfer functions differ from CAMB by:
        T_CAMB(k) = -T_CLASS(k)/k^2 """
    header="""Transfer functions T_i(k) for adiabatic (AD) mode (normalized to initial curvature=1)
d_i   stands for (delta rho_i/rho_i)(k,z) with above normalization
d_tot stands for (delta rho_tot/rho_tot)(k,z) with rho_Lambda NOT included in rho_tot
(note that this differs from the transfer function output from CAMB/CMBFAST, which gives the same
 quantities divided by -k^2 with k in Mpc^-1; use format=camb to match CAMB)
t_i   stands for theta_i(k,z) with above normalization
t_tot stands for (sum_i [rho_i+p_i] theta_i)/(sum_i [rho_i+p_i]))(k,z)
If some neutrino species are massless, or degenerate, the d_ncdm and t_ncdm columns may be missing below.
1:k (h/Mpc)              2:d_g                    3:d_b                    4:d_cdm                  5:d_ur        6:d_ncdm[0]              7:d_ncdm[1]              8:d_ncdm[2]              9:d_tot                 10:phi     11:psi                   12:h                     13:h_prime               14:eta                   15:eta_prime     16:t_g                   17:t_b                   18:t_ur        19:t_ncdm[0]             20:t_ncdm[1]             21:t_ncdm[2]             22:t_tot"""
    #This format matches the default output by CLASS command line.
    np.savetxt(transferfile, transfer, header=header)

if __name__ ==  "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('paramfile', type=str, help='genic paramfile')
    parser.add_argument('--extpk', type=str, help='optional external primordial power spectrum',required=False)
    parser.add_argument('--extraz', type=float,nargs='*', help='optional external primordial power spectrum',required=False)
    parser.add_argument('--verbose', action='store_true', help='print class runtime information',required=False)
    args = parser.parse_args()
    make_class_power(args.paramfile, args.extpk, args.extraz,args.verbose)