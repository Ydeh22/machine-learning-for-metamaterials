#least squares (levenberg-marquardt) to perform the inverse design problem in thin film metamaterials
#1 layer systems

from numba import jit
import numpy as np
import TMM_numba as tmm
from scipy.optimize import least_squares
import h5py
import matplotlib.pyplot as plt
import BB_metals as bb
import LD_metals as ld
import dielectric_materials as di
from joblib import Parallel, delayed
import multiprocessing
cores = multiprocessing.cpu_count()
import datetime
from tqdm import tqdm_notebook as tqdm
from numba.extending import overload

#label the main directory and an additional folder where you would like to save the output
#all files should be in this directory
dr = '/home/arl92/Documents/newdata/'
dr_save = '/home/arl92/Documents/newdata/comparisons/lsq/'

#import materials and define wavelength
#these parameters are the same found in the CNN data generation program
wave = np.linspace(450,950,200)*1E-9

#materials
ag = bb.nk_material('Ag',wave)
au = bb.nk_material('Au',wave)
al2o3 = np.loadtxt(dr+'alumina.txt').view(complex) #file containing RI constants taken from the dielectric_materials module
#identical to calling al2o3 = di.nk_material('al2o3',wave) for these wavelengths
d_tio2 = np.loadtxt(dr+'tio2.txt') #file containing RI constants taken from the dielectric_materials module
tio2 = d_tio2[0,:]+1j*d_tio2[1,:]
ito = np.loadtxt(dr+'ito.txt').view(complex) #file containing RI constants taken from the dielectric_materials module

gl = di.nk_Cauchy_Urbach(wave,1.55,0.005) #decent theoretical glass model based on cauchy disperision and previous experimental fits

#define the materials array
materials = np.array([ag,al2o3,ito,au,tio2])

#substrate and superstrate for the materials stack (1. = Void)
n_subst = gl
n_super = 1.

#model info taken from the data generation script
#this is to ensure the optimization is constrained to the same domain as probed by the CNN model
#since this data is just the ranges for the CNN dataset, inclusion of this data is necessary for a fair comparison between methods
trange = np.array([1,60])*1E-9 #allowed thicknesses
ang = np.array([25.,45.,65.])  #incident angles
num_ang = ang.size
max_its = 1000
num_lay = 1
num_mat = 5
num_wave = wave.size
np.random.seed(38947) #seed the RNG in numpy for reproducibility
#####

#data rescaling fcns
def resc_mat(in_mat):
    resc = in_mat / 4
    return resc
def resc_th(in_th):
    resc = in_th* 1E7
    return resc
def resc_ang(in_theta):
    resc = in_theta / 45
    return resc
def resc_psi(in_psi):
    resc = in_psi/90
    return resc
def resc_delt(in_delt):
    resc = in_delt/90
    return resc

#read in data and rescale output data tuple
def readin_data(filename,nmat,nang,nlay,nwave):
    f = h5py.File(filename,'r')
    arrd = np.array(f.get('data'))
    itl=0
    ith=nang
    theta = (arrd[:,itl:ith])
    itl=ith
    ith +=nmat
    ml1 = (arrd[:,itl:ith])
    itl=ith
    ith +=nlay
    th = (arrd[:,itl:ith])
    itl=ith
    ith +=nwave*nang
    rp = (arrd[:,itl:ith])
    itl=ith
    ith +=nwave*nang
    rs = (arrd[:,itl:ith])
    itl=ith
    ith +=nwave*nang
    tp = (arrd[:,itl:ith])
    itl=ith
    ith +=nwave*nang
    ts = (arrd[:,itl:ith])
    itl=ith
    ith +=nwave*nang
    psi = (arrd[:,itl:ith])
    itl=ith
    delta = (arrd[:,itl:])
    f.close()    
    return (ml1,th,theta,rp,rs,tp,ts,psi,delta)


#simple well in 2d, this can be used to test the ability of the script to find a global minimum
#attempt to optimize this function to check the vailidity of the code
def test_well(location):
    minwell =  np.abs(trange[0]-trange[1])/2+np.min((trange[0],trange[1]))
    x1 = location[0]
    x2 = location[1]
    height = np.array([(x1-minwell) ,(x2-minwell)])
    return height
    
#tanh transformation constrains the domain to conincide with the thickness ranges seen in the generation script file
#this keeps the otpimization script from optimizing to thicknesses which are outside the total range, and does not allow unphysical thicknesses which give technically correct spectra when transformed by the TMM code
#tanh(thickness_unscaled)  = [min_thickness, max_thickness] 
@jit(nopython=True)
def transform(x):
    xt = (np.tanh(x)*(0.5*(trange[1]-trange[0]))) + (0.5*(trange[1]+trange[0]))
    return xt

#residuals function, returns residuals between the optimum spectra and a TMM generated spectra based on the optimized thicknesses 
#different spectra are concatenated together in a linear array
@jit(nopython=True)
def residuals_fcn_pd(x,n,p,d):
    xt = transform(x)
    ang = np.array([25.,45.,65.])
    psi = np.zeros(wave.size*ang.size,dtype=np.float64)
    delta = np.zeros(wave.size*ang.size,dtype=np.float64)
    for j in range(0,ang.size):
        for i in range(0,wave.size):
            (psi[i+wave.size*j],delta[i+wave.size*j]) = tmm.ellips(ang[j], wave[i], n[:,i], xt, 1., n_subst[i])

    return np.concatenate(((psi-p),(delta-d)))

#generation function to be called by parallelization module
#performs the least squares optimization and any necessary globalization (over discrete materials subspaces).
#optimization is performed with the SCIPY least_squares module
#Fit type: 1 - 1 x LSQ per materials subspace
#          2 - numGlobal x LSQ per materials subspace

def gen_fcn_pd(g,fitType,numGlobal):
    p = tp[g,:]
    d = td[g,:]
    n = np.zeros((num_lay,num_wave),dtype=complex)
    #variables to hold the best structures and corrsponding fitnesses
    bmse = 1E10 
    bx0 = np.zeros(num_lay*2)
    #do a single LSQ instance in each materials subspace
    if fitType==1:
        mat = np.arange(num_mat)
        #uniform random thicknesses for initial guess
        x0 = np.random.uniform(trange[0],trange[1],size = (num_lay,))
        #loop over materials
        for i in range(num_mat):
            #define the structure with chosen materials
            n[0,:] = materials[i]
            #perform the LM LSQ optimization here
            plsq = least_squares(residuals_fcn_pd,x0,args=(n,p,d),method='lm',max_nfev=max_its)
            #calculate the MSE for the optimized thickness in this subspace
            mse = np.mean(residuals_fcn_pd(plsq.x,n,p,d)**2)
            #if this is the best MSE so far, save the structure and MSE
            #only saves the structure if the thicknesses are in the physical parameter range
            if mse <= bmse and np.all(plsq.x >= trange[0]) and np.all(plsq.x <= trange[1]):
                bmse = mse
                bx0 = np.concatenate((np.array([i]),plsq.x))
    #do multiple LSQ instances in each materials subspace, with random initialization each time
    elif fitType==2:
        mat = np.arange(num_mat)
        #complete this optimization numGlobal times in each subspace
        for iters in range(numGlobal):
        #uniform random thicknesses for initial guess
            x0 = np.random.uniform(trange[0],trange[1],size = (num_lay,))
            for i in range(num_mat):
                #define the structure with chosen materials
                n[0,:] = materials[i]
                #perform the LM LSQ optimization here
                plsq = least_squares(residuals_fcn_pd,x0,args=(n,p,d),method='lm',max_nfev=max_its)
                mse = np.mean(residuals_fcn_pd(plsq.x,n,p,d)**2)
                #if this is the best MSE so far, save the structure and MSE
                #only saves the structure if the thicknesses are in the physical parameter range
                if mse <= bmse and np.all(plsq.x >= trange[0]) and np.all(plsq.x <= trange[1]):
                    bmse = mse
                    bx0 = np.concatenate((np.array([i]),plsq.x))
    #return the best structure and the spectral RMSE
    return np.concatenate((bx0,np.sqrt(np.array([bmse]))))

#Accuracy for materials and thickness MSE metrics
#compared to the data generation materials and thicknesses
def accutest(tm,tt,pm,pt):
    (num_el,num_lay) = tm.shape
    acc = np.zeros(num_lay)
    thicks = np.zeros(num_lay)
    for i in range(num_el):
        for j in range(num_lay):
            if tm[i,j].astype(np.int64) == pm[i,j].astype(np.int64):
                acc[j] += 1.
            thicks[j] += (tt[i,j]-pt[i,j])**2
    thicks /= num_el
    thicks = np.sqrt(thicks)
    metrics = np.zeros(2*num_lay)
    metrics[:num_lay] = acc*100./num_el
    metrics[num_lay:] = thicks
    return metrics

#Accuracy for materials and thickness MSE metrics
#compared to the data generation materials and thicknesses
#this is sometimes needed to call instead of accutest. Performs the same function as accutest, but the tm.shape command has only one element, which can cause an error in accutest.
def accutest1(tm,tt,pm,pt):
    num_el = tm.shape
    acc = np.zeros(num_lay)
    thicks = np.zeros(num_lay)
    for i in range(num_el):
        for j in range(num_lay):
            if tm[i,j].astype(np.int64) == pm[i,j].astype(np.int64):
                acc[j] += 1.
            thicks[j] += (tt[i,j]-pt[i,j])**2
    thicks /= num_el
    thicks = np.sqrt(thicks)
    metrics = np.zeros(2*num_lay)
    metrics[:num_lay] = acc*100./num_el
    metrics[num_lay:] = thicks
    return metrics

#read in data
filename = dr+'data_rte_gen1lay5mat_0ge_240000n_v-tma_20201112.h5'
(tm1,tth,tang,trp,trs,ttp,tts,tp,td) = readin_data(filename,num_mat,num_ang,num_lay,num_wave)

#to initialize the numba functions
initialize = gen_fcn_pd(0,1,1)

#use arrays to choose the number of global points probed in loop
#makes finding the optimium nubmer of global points faster
#ftype: 1-single optimization in each subspace, 
#       2-random global with nglob points in each subspace
#nglob: for ftype=2 - number of global search points per materials subspace

ftype = [1,2,2,2,2]
nglob = [1,2,3,4,5]

# how many systems to probe and average over
systems = 1000
start = 220000 # look at the same examples as the network test dataset

#loop over all global optimization configurations
for i in range(len(ftype)):
    #print a nice headder for the log file
    print('\n\n','-'*10,'Starting, FT=%d, NG=%d'%(ftype[i],nglob[i]),'-'*10)
    sta = datetime.datetime.now()
    print(sta)
    #parallel call
    #parallelized by system, so each system runs independently on a core
    results= Parallel(n_jobs=cores)(delayed(gen_fcn_pd)(g,ftype[i],nglob[i]) for g in tqdm(range(start,start+systems)))
    end = datetime.datetime.now()
    #system runtime per system
    #particular to the number of parallel calls
    print('Runtime:',end-sta)
    sys_runtime = (end-sta)/systems
    print('Per System:',sys_runtime)


    #calcuate the metrics to find the algorithm optimzaiton performance in each configuration
    results = np.array(results)
    #calculate RMSE thickness and materials accuracy metrics
    metrics = accutest(np.transpose(np.array([np.argmax(tm1[start:start+systems,:],axis=1)])),tth[start:start+systems,:],results[:,:num_lay],results[:,num_lay:-1])
    print('Resutls:',metrics)
    #filename can be whatever you want
    #saves to the directory indicated with dr_save
    filename = dr_save + 'lsq_ellips_fitresults_1l5m_type'+str(ftype[i])+str(nglob[i])+'_'+sta.strftime('%y')+sta.strftime('%m')+sta.strftime('%d')+'_'+sta.strftime('%H')+sta.strftime('%M')+sta.strftime('%S')
    #runtime per system averaged over all systems
    #this is particular to the number of parallel workers, typically the nubmer of nodes in the system
    sys_runtime_f = np.array([sys_runtime.seconds+sys_runtime.microseconds*1E-6]).astype('float')
    print(filename)
    #save the results
    np.savetxt(filename+'.txt',np.concatenate((np.reshape(metrics,(2*num_lay,)),sys_runtime_f)),delimiter=',')
    np.savetxt(filename+'_results.txt',results,delimiter=',')


