# IBWT-Risk-Simulation-Model

This repository provides the data and code for simulating risk transmission in the operation planning of inter-basin water transfer systems. 

The model evaluates how water diversion decisions affect the generation, transmission, and redistribution of water shortage and spill risks across interconnected basins. It performs water allocation simulations under multiple joint inflow–demand scenarios and summarizes the resulting risk indicators at both system and regional levels.

## Repository Contents

* `IBWT-Risk-Simulation-Model.py`
  Main Python script for generating diversion-plan scenarios, performing water allocation simulations, and calculating shortage and spill risks.

* `Input_Data.xlsx`
  Input dataset containing joint inflow–demand scenarios, diversion plans, user water demands, lake characteristics, pumping-station parameters, operating water-level constraints, and other model boundary conditions.

* `README.md`
  Description of the model, input data, dependencies, and execution procedure.

## Input Data

The input data are stored in `Input_Data.xlsx` and organized into the following worksheets:

* `Lakes`: Initial storage, water-supply capacity, and other lake parameters.
* `Pumps`: Pumping capacities and conveyance-loss coefficients.
* `DiversionPlan`: Baseline monthly diversion plans.
* `Users`: User codes, regional groups, and water-supply priorities.
* `LakeLevels`: Monthly minimum, maximum, and intermediate lake-storage limits.
* `LakeInflow`: Lake inflow scenarios.
* `Demand`: Water-demand scenarios for individual users.

## Installation and Dependencies

The model was developed in Python. Python is a free and open-source programming language widely used for scientific computing and data analysis.

Python can be obtained from:

https://www.python.org/

The following software and packages are required:

* Python 3.8 or later
* NumPy
* Pandas
* OpenPyXL

The required packages can be installed using:

```bash
pip install numpy pandas openpyxl
```

## Data Availability

The processed input data required to run the risk simulation model are provided in `Input_Data.xlsx`.

The original inflow, water-demand, and engineering-operation data related to the Eastern Route of the South-to-North Water Diversion Project can be obtained from the official website of China South-to-North Water Diversion Group Co., Ltd.:

http://www.csnwd.com.cn/
