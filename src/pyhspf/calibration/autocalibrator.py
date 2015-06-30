# autocalibrator.py
#
# David J. Lampert (djlampert@gmail.com)
#
# Contains the AutoCalibrator class that can be used to calibrate a model.
# The class requires and HSPFModel class, start and end dates, and an output
# location to work in while running simulations. The primary function is
# autocalibrate, and it takes a list of HSPF variables, perturbations (as a
# percentage, optimization parameter, and flag for parallelization as 
# keyword arguments. The calibration routine can be summarized as follows:
# 1. Set up a series of simulations with a small perturbation to the current
#    parameter values for the parameters of interest
# 2. Make copies of the input HSPFModel and adjust the parameter values
# 3. Run the simulations and get the effect of the optimization parameter
# 4. Adjust the baseline parameter values if they improve performance
# 5. Repeat until a maximum is achieved.

# The class should be adaptable to other methodologies.

import os, pickle, datetime, time, numpy

from multiprocessing import Pool, cpu_count
from pyhspf.core import HSPFModel, Postprocessor

class AutoCalibrator:
    """
    A class to use to autocalibrate an HSPF model.
    """

    def __init__(self, 
                 hspfmodel, 
                 start, 
                 end, 
                 output, 
                 comid = None,
                 gageid = None,
                 atemp = False,
                 snow = False,
                 hydrology = False,
                 warmup = 30,
                 parameter_ranges = {'IRC':    (0.5,   2),
                                     'LZETP':  (0.2,  1.4),
                                     'DEEPFR': (0,      1),
                                     'LZSN':   (0.2,   10),
                                     'UZSN':   (0.2,   10),
                                     'INFILT': (0.01,  20),
                                     'INTFW':  (0.01,  10),
                                     'AGWRC':  (0.5,    2),
                                     },
                 ):

        self.hspfmodel        = hspfmodel
        self.start            = start
        self.end              = end
        self.output           = output
        self.gageid           = gageid
        self.comid            = comid
        self.atemp            = atemp
        self.snow             = snow
        self.hydrology        = hydrology
        self.warmup           = warmup
        self.parameter_ranges = parameter_ranges

    def copymodel(self,
                  name,
                  verbose = True,
                  ):
        """
        Returns a copy of the HSPFModel.
        """
        
        with open(self.hspfmodel, 'rb') as f: hspfmodel = pickle.load(f)

        hspfmodel.filename = name

        return hspfmodel

    def submodel(self, 
                  name,
                  verbose = True,
                  ):
        """
        Returns a copy of the HSPFModel.
        """

        model = HSPFModel()
        model.build_from_existing(self.hspfmodel, name)

        # turn on the modules

        if self.atemp:     model.add_atemp()
        if self.snow:      model.add_snow()
        if self.hydrology: model.add_hydrology()

        # add the time series

        for f in self.hspfmodel.flowgages:
            start, tstep, data = self.hspfmodel.flowgages[f]
            model.add_timeseries('flowgage', f, start, data, tstep = tstep)

        for p in self.hspfmodel.precipitations: 
            start, tstep, data = self.hspfmodel.precipitations[p]
            model.add_timeseries('precipitation', p, start, data, tstep = tstep)

        for e in self.hspfmodel.evaporations: 
            start, tstep, data = self.hspfmodel.evaporations[e]
            model.add_timeseries('evaporation', e, start, data, tstep = tstep)

        for t in self.hspfmodel.temperatures:
            start, tstep, data = self.hspfmodel.temperatures[t]
            model.add_timeseries('temperature', t, start, data, tstep = tstep)

        for t in self.hspfmodel.dewpoints:
            start, tstep, data = self.hspfmodel.dewpoints[t]
            model.add_timeseries('dewpoint', t, start, data, tstep = tstep)

        for t in self.hspfmodel.windspeeds:
            start, tstep, data = self.hspfmodel.windspeeds[t]
            model.add_timeseries('wind', t, start, data, tstep = tstep)

        for t in self.hspfmodel.solars:
            start, tstep, data = self.hspfmodel.solars[t]
            model.add_timeseries('solar', t, start, data, tstep = tstep)

        for t in self.hspfmodel.snowfalls:
            start, tstep, data = self.hspfmodel.snowfalls[t]
            model.add_timeseries('snowfall', t, start, data, tstep = tstep)

        for t in self.hspfmodel.snowdepths:
            start, tstep, data = self.hspfmodel.snowdepths[t]
            model.add_timeseries('snowdepth', t, start, data, tstep = tstep)

        for tstype, identifier in self.hspfmodel.watershed_timeseries.items():

            model.assign_watershed_timeseries(tstype, identifier)

        for tstype, d in self.hspfmodel.subbasin_timeseries.items():

            for subbasin, identifier in d.items():
                
                if subbasin in model.subbasins:

                    model.assign_subbasin_timeseries(tstype, subbasin, 
                                                     identifier)

        for tstype, d in self.hspfmodel.landuse_timeseries.items():

            for luc, identifier in d.items():

                model.assign_landuse_timeseries(tstype, luc, identifier)

        return model

    def adjust(self, model, variable, adjustment):
        """
        Adjusts the values of the given parameter for all the PERLNDs in the
        watershed by the "adjustment." The adjustments can be defined as 
        values relative to the default (products) or absolute values (sums).
        """ 

        if variable == 'LZSN':
            for p in model.perlnds: p.LZSN   *= adjustment
        if variable == 'UZSN':
            for p in model.perlnds: p.UZSN   *= adjustment
        if variable == 'LZETP':
            for p in model.perlnds: p.LZETP  *= adjustment
        if variable == 'INFILT':
            for p in model.perlnds: p.INFILT *= adjustment
        if variable == 'INTFW':
            for p in model.perlnds: p.INTFW  *= adjustment
        if variable == 'IRC':
            for p in model.perlnds: p.IRC    *= adjustment
        if variable == 'AGWRC':
            for p in model.perlnds: p.AGWRC  *= adjustment
        if variable == 'DEEPFR':
            for p in model.perlnds: p.DEEPFR += adjustment
    
    def run(self, 
            model,
            targets = ['reach_outvolume'],
            verbose = False,
            ):
        """
        Creates a copy of the base model, adjusts a parameter value, runs
        the simulation, calculates and returns the perturbation.
        """

        # build the input files and run

        model.build_wdminfile()
        model.warmup(self.start, days = self.warmup, atemp = self.atemp, 
                     snow = self.snow,
                     hydrology = self.hydrology)
        model.build_uci(targets, self.start, self.end, atemp = self.atemp,
                        snow = self.snow, hydrology = self.hydrology)
        model.run(verbose = verbose)

        # get the regression information using the postprocessor

        p = Postprocessor(model, (self.start, self.end), comid = self.comid)

        # get the daily flows across the calibration period

        stimes, sflows = p.get_sim_flow(self.comid, tstep = 'daily',
                                        dates = (self.start, self.end))
        otimes, oflows = p.get_obs_flow(tstep = 'daily', 
                                        dates = (self.start, self.end))

        # close the postprocessor

        p.close()

        # remove points with missing data from both simulated and oberved flows

        sflows = [sflows[stimes.index(t)] 
                  for t, f in zip(otimes, oflows) 
                  if t in stimes and f is not None]
        oflows = [oflows[otimes.index(t)] 
                  for t, f in zip(otimes, oflows) 
                  if f is not None]

        # return the appropriate performance metric

        if self.optimization == 'Nash-Sutcliffe Product':

            # daily log flows

            log_o = [numpy.log(f) for f in oflows]
            log_s = [numpy.log(f) for f in sflows]

            logdNS = (1 - sum((numpy.array(log_s) - numpy.array(log_o))**2) /
                      sum((numpy.array(log_o) - numpy.mean(log_o))**2))

            # daily NS

            dNS  = (1 - sum((numpy.array(sflows) - numpy.array(oflows))**2) /
                    sum((numpy.array(oflows) - numpy.mean(oflows))**2))

            return dNS * logdNS

        if self.optimization == 'Nash-Sutcliffe Efficiency': 

            # daily NS

            dNS  = (1 - sum((numpy.array(sflows) - numpy.array(oflows))**2) /
                    sum((numpy.array(oflows) - numpy.mean(oflows))**2))

            return dNS

    def simulate(self, simulation):
        """
        Performs a simulation and returns the optimization value.
        """

        name, perturbation, adjustments = simulation

        # create a copy of the original HSPFModel to modify

        filename = '{}/{}{:4.3f}'.format(self.output, name, perturbation)

        model = self.copymodel(filename)
                                         
        # adjust the values of the parameters

        for variable, adjustment in zip(self.variables, adjustments):
            self.adjust(model, variable, adjustment)

        # run and pass back the result
                  
        print('running', name, 'perturbation')
        return self.run(model)

    def perturb(self, 
                parallel,
                nprocessors,
                timeout = 300,
                verbose = True,
                ):
        """
        Performs the perturbation analysis.
        """

        if verbose:
            st = time.time()
            if parallel:
                print('perturbing the model in parallel\n')
            else:
                print('perturbing the model serially\n')

        # adjust the parameter values for each variable for each simulation

        its = range(len(self.variables)), self.variables, self.perturbations
        adjustments = []
        for i, v, p in zip(*its):
            adjustment = self.values[:]
            adjustment[i] += p
            adjustments.append(adjustment)
                                 
        # run a baseline simulation and perturbation simulations for 
        # each of calibration variables

        its = self.variables, self.perturbations, adjustments
        simulations = ([['baseline', 0, self.values]] + 
                       [[v, p, a] for v, p, a in zip(*its)])

        if parallel:

            if nprocessors is None: n = cpu_count()
            else:                   n = nprocessors

            try: 

                # create a pool of workers and try parallel

                with Pool(n, maxtasksperchild = 4 * cpu_count()) as p:
                    results = p.map_async(self.simulate, simulations)
                    optimizations = results.get(timeout = timeout)

            except:

                print('error: parallel calibration failed\n')
                print('last values of calibration variables:\n')
                for i in zip(self.variables, self.values): print(*i)
                raise RuntimeError

        else:

            # run the simulations to get the optimization parameter values

            optimizations = [self.simulate(s) for s in simulations]

        if verbose: 

            print('\ncompleted perturbation in ' +
                  '{:.1f} seconds\n'.format(time.time() - st))

        # calculate the sensitivities for the perturbations

        sensitivities = [o - optimizations[0] for o in optimizations[1:]]

        # save the current value of the optimization parameter

        self.value = optimizations[0]

        return sensitivities

    def get_default(self, variable):
        """Gets the default value of the perturbation for the variable.
        The defaults are based on experience with parameter sensitivity."""

        if   variable == 'LZSN':   return 0.05
        elif variable == 'UZSN':   return 0.05
        elif variable == 'LZETP':  return 0.02
        elif variable == 'INFILT': return 0.04
        elif variable == 'INTFW':  return 0.01
        elif variable == 'IRC':    return 0.02
        elif variable == 'AGWRC':  return 0.005
        elif variable == 'DEEPFR': return 0.01
        else:
            print('error: unknown variable specified\n')
            raise

    def check_variables(self):
        """User-defined check on the values of the variables to ensure 
        the calibrated values stay within the limits."""

        for i in range(len(self.variables)):

            variable = self.variables[i]
            value    = self.values[i]
            mi, ma   = self.parameter_ranges[variable]
            
            if value < mi:
                its = variable, value, mi
                print('warning: current value of ' +
                      '{} ({}) is below minimum ({})'.format(*its))
                self.values[i] = mi
            if value > ma:
                its = variable, value, ma
                print('warning: current value of ' +
                      '{} ({}) is above maximum ({})'.format(*its))
                self.values[i] = ma

    def optimize(self, parallel, nprocessors):
        """
        Optimizes the objective function for the parameters.
        """

        current = self.value - 1
        t = 'increasing {:6s} {:>5.1%} increases {} {:6.3f}'
        while current < self.value:

            # update the current value

            current = self.value

            print('\ncurrent optimization value: {:4.3f}\n'.format(self.value))

            # perturb the values positively

            sensitivities = self.perturb(parallel, nprocessors)

            # iterate through the calibration variables and update if they
            # improve the optimization parameter

            for i in range(len(self.values)):

                if sensitivities[i] > 0: 

                    self.values[i] = round(self.values[i] + 
                                           self.perturbations[i], 3)

                its = (self.variables[i], self.perturbations[i], 
                       self.optimization, sensitivities[i])
                print(t.format(*its))

            print('')

            # perturb the values negatively

            self.perturbations = [-p for p in self.perturbations]
            sensitivities = self.perturb(parallel, nprocessors)

            # iterate through the calibration variables and update if they
            # improve the optimization parameter

            for i in range(len(self.values)):

                if sensitivities[i] > 0:

                    self.values[i] = round(self.values[i] + 
                                           self.perturbations[i], 3)
           
                its = (self.variables[i], self.perturbations[i], 
                       self.optimization, sensitivities[i])
                print(t.format(*its))

            # reset the perturbations to positive

            self.perturbations = [-p for p in self.perturbations]

            # make sure variables are within bounds

            self.check_variables()

            # show progress

            print('calibration values relative to default:\n')
            for variable, adjustment in zip(self.variables, self.values):
                print('{:6s} {:5.3f}'.format(variable, adjustment))

    def autocalibrate(self, 
                      output,
                      variables = {'LZSN':   1.,
                                   'UZSN':   1.,
                                   'LZETP':  1.,
                                   'INFILT': 1.,
                                   'INTFW':  1.,
                                   'IRC':    1.,
                                   'AGWRC':  1.,
                                   },
                      optimization = 'Nash-Sutcliffe Efficiency',
                      perturbations = [2, 1, 0.5],
                      parallel = True,
                      nprocessors = None,
                      ):
        """
        Autocalibrates the hydrology for the hspfmodel by modifying the 
        values of the HSPF PERLND parameters contained in the vars list.
        """

        # find the comid of the calibration gage

        if self.comid is None and self.gageid is not None:

            # open up the base model

            with open(self.hspfmodel, 'rb') as f: hspfmodel = pickle.load(f)

            # make a dictionary to use to find the comid for each gage id

            d = {v:k 
                 for k, v in hspfmodel.subbasin_timeseries['flowgage'].items()}
            self.comid = d[self.gageid]

        elif self.comid is None:

            # then just use the outlet

            print('error, no calibration gage specified')
            raise

        # set up the current values of the variables, the amount to perturb
        # them by in each iteration, and the optimization parameter

        self.variables    = [v for v in variables]
        self.values       = [variables[v] for v in variables]
        self.optimization = optimization

        # current value of the optimization parameter

        self.value = -10 

        # perturb until reaching a maximum (start with large perturbations)

        print('\nattempting to calibrate {}'.format(self.hspfmodel))
        for p in perturbations:

            self.perturbations = [p * self.get_default(v) for v in variables]
            self.optimize(parallel, nprocessors)

        print('\noptimization complete, saving model\n')

        model = self.copymodel('calibrated')
        model.add_hydrology()

        # run the model to save the warmed up input parameters

        self.run(model)
                                 
        # adjust the values of the parameters

        print('calibration values relative to default:\n')
        for variable, adjustment in zip(self.variables, self.values):
            self.adjust(model, variable, adjustment)
            print('{:6s} {:5.3f}'.format(variable, adjustment))

        with open(output, 'wb') as f: pickle.dump(model, f)
