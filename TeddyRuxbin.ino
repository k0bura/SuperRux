/******************************************************************************
TestRun.ino
TB6612FNG H-Bridge Motor Driver Example code
Michelle @ SparkFun Electronics
8/20/16
https://github.com/sparkfun/SparkFun_TB6612FNG_Arduino_Library

Uses 2 motors to show examples of the functions in the library.  This causes
a robot to do a little 'jig'.  Each movement has an equal and opposite movement
so assuming your motors are balanced the bot should end up at the same place it
started.

Resources:
TB6612 SparkFun Library

Development environment specifics:
Developed on Arduino 1.6.4
Developed with ROB-9457
******************************************************************************/

// This is the library for the TB6612 that contains the class Motor and all the
// functions
#include <SparkFun_TB6612.h>

// Pins for all inputs, keep in mind the PWM defines must be on PWM pins
// the default pins listed are the ones used on the Redbot (ROB-12097) with
// the exception of STBY which the Redbot controls with a physical switch
#define AIN1 2
#define BIN1 7
#define AIN2 4
#define BIN2 8
#define PWMA 5
#define PWMB 6
#define STBY 9

#define CIN1 12
#define CIN2 10
#define PWMC 11
#define STBYC 3

// these constants are used to allow you to make your motor configuration 
// line up with function names like forward.  Value can be 1 or -1
const int offsetA = 1;
const int offsetB = 1;
const int offsetC = 1;

// Initializing motors.  The library will allow you to initialize as many
// motors as you have memory for.  If you are using functions like forward
// that take 2 motors as arguements you can either write new functions or
// call the function more than once.
Motor upperMouth = Motor(AIN1, AIN2, PWMA, offsetA, STBY);
Motor lowerMouth = Motor(BIN1, BIN2, PWMB, offsetB, STBY);
Motor eyes       = Motor(CIN1, CIN2, PWMC, offsetC, STBYC);

//declare constants for timer
long previousMillis = 0;
long interval = 15000; // timer is set to 15 second intervals

void setup()
{ 
  Serial.begin(9600);
 //lowerMouth.drive(100);
 //close mouth to set netural position
   neutral();
   delay(1000);
  
  testTalk();
  eyeBlink();
  delay(1000);
  testEyes();
  eyeBlink();
  neutral();
  testTalk();
  neutral();
  
}


void loop(){
  //Set timer for eyeblinks (every 1llis > interval){
    //previousMillis = curr5 seconds)
  //unsigned long currentMillis = millis();
  //if(currentMillis - previousMientMillis;
  //eyeBlink();
  //}

  int sensorValue = analogRead(A1);
  //Serial.println(sensorValue);
  //delay(100);
 }

void testTalk(){
    //Open wide
  openMouth(200);
  delay(250);
  
  stopMouth();

  
  //close a little
  closeMouth(150);
  delay(200);
  //stopMouth();

  //Open a little
  openMouth(100);
  delay(500);
  
  stopMouth();
  delay(200); //stay open

//close a little
  closeMouth(100);
  delay(500);
  
  //neutral();
}

void testEyes(){
  //lookup
  eyesUp(80);
  delay(1200);
  
  eyesDown(70);
  delay(1100);

  eyesUp(63);
  delay(1100);
  
  stopEyes();
}

void neutral(){
  upperMouth.drive(-200);
  lowerMouth.drive(200);
  eyes.drive(-200);
  delay(500);
  upperMouth.brake();
  lowerMouth.brake();

  eyes.drive(200);
  delay(300);
  eyes.brake();
}

void closeMouth(int speed){
  upperMouth.drive(-speed);
  lowerMouth.drive(speed);
}

void openMouth(int speed){
  upperMouth.drive(speed);
  lowerMouth.drive(-speed);
}

void eyeBlink(){
  eyesDown(255);
  delay(350);
  stopEyes();
  
  delay(200); //close them for a bit
  
  eyesUp(200);
  delay(300);
  stopEyes();
  
 // delay(1200); //interval between blinks
 
}

void eyesUp(int speed){
  eyes.drive(speed);
}

void eyesDown(int speed){
  eyes.drive(-speed);
}

void stopMouth(){
  upperMouth.brake();
  lowerMouth.brake();
}

void stopEyes(){
  eyes.brake();
}
